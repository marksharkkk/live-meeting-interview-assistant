import asyncio
import json
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

try:
    from .config import persist_settings, settings
    from .knowledge_base import KnowledgeBase
    from .llm_service import LLMService
    from .security import MAX_UPLOAD_BYTES, safe_upload_path
    from .voice_recognition import VoiceRecognizer
except ImportError:
    from config import persist_settings, settings
    from knowledge_base import KnowledgeBase
    from llm_service import LLMService
    from security import MAX_UPLOAD_BYTES, safe_upload_path
    from voice_recognition import VoiceRecognizer


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
API_TOKEN = os.environ.get("MEETING_ASSISTANT_TOKEN", "")

voice_recognizer = VoiceRecognizer(
    language=settings.voice_language,
    sample_rate=settings.voice_sample_rate,
)
voice_recognizer.set_input_device(
    None if settings.voice_input_device < 0 else settings.voice_input_device
)
llm_service = LLMService()
knowledge_base = KnowledgeBase()
voice_connection_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await asyncio.to_thread(voice_recognizer.cleanup)


app = FastAPI(
    title="Meeting Assistant API",
    version="1.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Meeting-Assistant-Token"],
)


@app.middleware("http")
async def authenticate_local_api(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    if not API_TOKEN:
        return JSONResponse({"detail": "Local API token is not configured"}, status_code=503)
    supplied = request.headers.get("X-Meeting-Assistant-Token", "")
    if not secrets.compare_digest(supplied, API_TOKEN):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=10000)
    use_knowledge_base: bool = True
    system_prompt: Optional[str] = Field(default=None, max_length=8000)
    conversation_history: list[ConversationMessage] = Field(default_factory=list, max_length=10)
    regenerate: bool = False


class SettingsUpdate(BaseModel):
    openai_api_key: Optional[str] = Field(default=None, max_length=500)
    openai_api_base: Optional[str] = Field(default=None, max_length=1000)
    openai_model: Optional[str] = Field(default=None, max_length=200)
    voice_language: Optional[str] = Field(default=None, max_length=20)
    voice_input_device: int = Field(default=-1, ge=-1, le=1000)
    clear_openai_api_key: bool = False

    @field_validator("openai_api_base")
    @classmethod
    def validate_api_base(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("API Base URL must be a valid HTTP or HTTPS URL")
        if parsed.username or parsed.password:
            raise ValueError("Credentials are not allowed in the API Base URL")
        return value.strip().rstrip("/")


class KnowledgeTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500_000)
    metadata: Optional[dict] = None


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "api_key_configured": bool(settings.openai_api_key)}


def _websocket_token(websocket: WebSocket) -> str:
    protocols = [
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]
    if len(protocols) >= 2 and protocols[0] == "meeting-assistant":
        return protocols[1]
    return ""


@app.websocket("/ws/voice")
async def websocket_voice(websocket: WebSocket):
    supplied_token = _websocket_token(websocket)
    if not API_TOKEN or not secrets.compare_digest(supplied_token, API_TOKEN):
        await websocket.close(code=1008, reason="Unauthorized")
        return
    if voice_connection_lock.locked():
        await websocket.accept(subprotocol="meeting-assistant")
        await websocket.send_json({"type": "error", "message": "Voice recognition is already in use"})
        await websocket.close(code=1013)
        return

    async with voice_connection_lock:
        await websocket.accept(subprotocol="meeting-assistant")
        transcript_queue: asyncio.Queue[dict] = asyncio.Queue()
        event_loop = asyncio.get_running_loop()

        def enqueue(message: dict) -> None:
            event_loop.call_soon_threadsafe(transcript_queue.put_nowait, message)

        try:
            await websocket.send_json({"type": "status", "message": "Loading speech model"})
            await asyncio.to_thread(
                voice_recognizer.start_listening,
                lambda text: enqueue({"type": "transcript", "text": text}),
                lambda message: enqueue({"type": "error", "message": message}),
            )
            await websocket.send_json({"type": "status", "message": "Listening"})

            while True:
                queue_task = asyncio.create_task(transcript_queue.get())
                receive_task = asyncio.create_task(websocket.receive_text())
                done, pending = await asyncio.wait(
                    {queue_task, receive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()

                if queue_task in done:
                    message = queue_task.result()
                    await websocket.send_json(message)
                    if message.get("type") == "error":
                        break
                else:
                    message = json.loads(receive_task.result())
                    if message.get("type") == "stop":
                        await websocket.send_json({"type": "stopped"})
                        break
        except Exception as exc:
            logger.info("Voice WebSocket closed: %s", exc)
        finally:
            await asyncio.to_thread(voice_recognizer.stop_listening)
            try:
                await websocket.close()
            except Exception:
                pass


@app.post("/api/answer")
async def generate_answer(request: QuestionRequest):
    context = await knowledge_base.query(request.question) if request.use_knowledge_base else ""
    try:
        answer = await llm_service.generate_answer(
            question=request.question,
            context=context,
            system_prompt=request.system_prompt or "",
        )
        return {"answer": answer, "context_used": bool(context)}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


def _sse_event(event_type: str, **payload) -> str:
    return "data: " + json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n\n"


@app.post("/api/answer/stream")
async def generate_answer_stream(request: QuestionRequest):
    async def event_generator():
        context = await knowledge_base.query(request.question) if request.use_knowledge_base else ""
        history = [message.model_dump() for message in request.conversation_history]
        try:
            async for token in llm_service.generate_answer_stream(
                question=request.question,
                context=context,
                system_prompt=request.system_prompt or "",
                conversation_history=history,
                temperature=1.1 if request.regenerate else 0.7,
            ):
                yield _sse_event("token", content=token)
                await asyncio.sleep(0)
            yield _sse_event("done")
        except RuntimeError as exc:
            yield _sse_event("error", message=str(exc))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _store_upload(file: UploadFile) -> tuple[Path | None, dict | None]:
    try:
        target = safe_upload_path(knowledge_base.uploads_dir, file.filename or "")
    except ValueError as exc:
        return None, {"file": file.filename or "", "status": "failed", "reason": str(exc)}

    temp_path = knowledge_base.uploads_dir / f".upload-{uuid.uuid4().hex}.tmp"
    written = 0
    try:
        with temp_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise ValueError("File is larger than the 25 MB limit")
                destination.write(chunk)
        os.replace(temp_path, target)
        return target, None
    except (OSError, ValueError) as exc:
        temp_path.unlink(missing_ok=True)
        return None, {"file": target.name, "status": "failed", "reason": str(exc)}
    finally:
        await file.close()


@app.post("/api/knowledge/files")
async def add_knowledge_files(files: list[UploadFile] = File(...)):
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Upload at most 20 files at a time")

    stored_paths = []
    failed = []
    for file in files:
        path, error = await _store_upload(file)
        if path:
            stored_paths.append(str(path))
        if error:
            failed.append(error)

    result = await knowledge_base.add_documents(stored_paths) if stored_paths else {
        "success": False,
        "message": "没有可加载的文件",
        "files": [],
    }
    result["files"].extend(failed)
    return result


@app.post("/api/knowledge/text")
async def add_knowledge_text(request: KnowledgeTextRequest):
    await knowledge_base.add_text(request.text.strip(), request.metadata)
    return {"success": True, "message": "Text added to knowledge base"}


@app.delete("/api/knowledge")
async def clear_knowledge_base():
    await knowledge_base.clear(remove_uploads=True)
    return {"success": True, "message": "Knowledge base and uploaded files cleared"}


@app.get("/api/knowledge")
async def get_knowledge_status():
    return knowledge_base.status()


@app.delete("/api/knowledge/files/{filename}")
async def delete_knowledge_file(filename: str):
    try:
        file_path = safe_upload_path(knowledge_base.uploads_dir, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    file_path.unlink()
    removed = knowledge_base.delete_source(file_path)
    return {"success": True, "message": f"Deleted {filename}", "removed_chunks": removed}


@app.post("/api/knowledge/cleanup")
async def cleanup_knowledge_cache():
    removed = knowledge_base.cleanup_missing_sources()
    return {
        "success": True,
        "message": f"清理了 {len(removed)} 个无效文件记录",
        "removed_files": removed,
        "remaining_chunks": knowledge_base.status()["document_count"],
    }


@app.post("/api/knowledge/reload")
async def reload_unloaded_files():
    loaded = knowledge_base.loaded_source_names()
    unloaded = [
        str(path)
        for path in knowledge_base.uploads_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".pdf", ".txt", ".docx"}
        and path.name.casefold() not in loaded
    ]
    if not unloaded:
        return {"success": True, "message": "所有文件均已加载", "files": []}
    return await knowledge_base.add_documents(unloaded)


@app.post("/api/settings")
async def update_settings(request: SettingsUpdate):
    updates = {}
    if request.clear_openai_api_key:
        updates["openai_api_key"] = ""
    elif request.openai_api_key is not None and request.openai_api_key.strip():
        updates["openai_api_key"] = request.openai_api_key.strip()
    if request.openai_api_base is not None:
        updates["openai_api_base"] = request.openai_api_base
    if request.openai_model is not None and request.openai_model.strip():
        updates["openai_model"] = request.openai_model.strip()
    if request.voice_language is not None and request.voice_language.strip():
        updates["voice_language"] = request.voice_language.strip()
    if request.voice_input_device is not None:
        updates["voice_input_device"] = request.voice_input_device

    try:
        persist_settings(updates)
    except OSError:
        logger.exception("Could not persist settings")
        raise HTTPException(status_code=500, detail="Could not save settings") from None

    llm_service.refresh_client()
    voice_recognizer.set_language(settings.voice_language)
    voice_recognizer.set_input_device(
        None if settings.voice_input_device < 0 else settings.voice_input_device
    )
    return {"success": True, "message": "Settings saved"}


@app.get("/api/audio/devices")
async def list_audio_devices():
    try:
        devices = await asyncio.to_thread(voice_recognizer.list_input_devices)
    except Exception as exc:
        logger.warning("Could not enumerate audio devices: %s", exc)
        devices = []
    return {"devices": devices}


@app.get("/api/settings")
async def get_settings():
    return {
        "api_key_configured": bool(settings.openai_api_key),
        "openai_api_base": settings.openai_api_base,
        "openai_model": settings.openai_model,
        "voice_language": settings.voice_language,
        "voice_input_device": settings.voice_input_device,
    }


if __name__ == "__main__":
    if not API_TOKEN:
        raise RuntimeError("MEETING_ASSISTANT_TOKEN must be supplied by the desktop app")
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
