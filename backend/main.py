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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

try:
    from .config import persist_settings, settings
    from .knowledge_base import KnowledgeBase
    from .llm_service import LLMService
    from .meeting_store import MeetingStore
    from .security import MAX_UPLOAD_BYTES, safe_upload_path
    from .speech_model import speech_model
    from .voice_recognition import VoiceRecognizer
except ImportError:
    from config import persist_settings, settings
    from knowledge_base import KnowledgeBase
    from llm_service import LLMService
    from meeting_store import MeetingStore
    from security import MAX_UPLOAD_BYTES, safe_upload_path
    from speech_model import speech_model
    from voice_recognition import VoiceRecognizer


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
API_TOKEN = os.environ.get("MEETING_ASSISTANT_TOKEN", "")

voice_recognizer = VoiceRecognizer(
    language=settings.voice_language,
    sample_rate=settings.voice_sample_rate,
)
voice_recognizer.set_input_device(
    None if settings.voice_input_device == -1 else settings.voice_input_device
)
llm_service = LLMService()
knowledge_base = KnowledgeBase()
voice_connection_lock = asyncio.Lock()
meeting_store = MeetingStore()
voice_recognizer.audio_callback = meeting_store.audio
summary_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await asyncio.to_thread(voice_recognizer.cleanup)
    meeting_store.close_audio()


app = FastAPI(
    title="Meeting Assistant API",
    version="0.0.1",
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
    meeting_id: Optional[str] = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    source_at: Optional[str] = Field(default=None, max_length=80)
    answer_language: Literal['auto', 'zh', 'en'] = 'auto'


class TranslationRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    meeting_id: Optional[str] = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    source_at: Optional[str] = Field(default=None, max_length=80)
    target: Literal['auto', 'zh', 'en'] = 'auto'
    # Interim subtitle previews must not be written as if they were final
    # translations.  The UI sets this false for previews and leaves the
    # default true for the completed sentence.
    persist: bool = True


class SettingsUpdate(BaseModel):
    openai_api_key: Optional[str] = Field(default=None, max_length=500)
    openai_api_base: Optional[str] = Field(default=None, max_length=1000)
    openai_model: Optional[str] = Field(default=None, max_length=200)
    voice_language: Optional[str] = Field(default=None, max_length=20)
    voice_input_device: Optional[int] = Field(default=None, ge=-2, le=1000)
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


@app.get("/api/speech-model")
async def speech_model_status():
    return speech_model.status()


@app.post("/api/speech-model/download")
async def download_speech_model():
    return speech_model.start_download()


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
            if message.get('type') == 'transcript':
                row = meeting_store.append(message['text'])
                if row:
                    message.update(source_id=row['id'], source_at=row['created_at'])
            event_loop.call_soon_threadsafe(transcript_queue.put_nowait, message)

        def enqueue_transcript(text: str) -> None:
            enqueue({'type': 'transcript', 'text': text, 'end_of_utterance': False})

        def enqueue_boundary(text: str, end_of_utterance: bool) -> None:
            # The recognition callback is queued before this metadata callback.
            # Mark the matching latest item without changing the durable text.
            event_loop.call_soon_threadsafe(
                lambda: transcript_queue.put_nowait(
                    {'type': 'transcript-boundary', 'end_of_utterance': end_of_utterance}
                )
            )

        stop_requested = False
        try:
            await websocket.send_json({"type": "status", "message": "Loading speech model"})
            await asyncio.to_thread(
                voice_recognizer.start_listening,
                enqueue_transcript,
                lambda message: enqueue({"type": "error", "message": message}),
                enqueue_boundary,
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
                await asyncio.gather(*pending, return_exceptions=True)

                if queue_task in done:
                    message = queue_task.result()
                    await websocket.send_json(message)
                    if message.get("type") == "error":
                        break
                if receive_task in done:
                    message = json.loads(receive_task.result())
                    if message.get("type") == "stop":
                        logger.info('Voice stop requested by client')
                        stop_requested = True
                        break
        except Exception as exc:
            logger.info("Voice WebSocket closed: %s", exc)
            try:
                await websocket.send_json({"type": "error", "message": str(exc)})
            except Exception:
                pass
        finally:
            await asyncio.to_thread(voice_recognizer.stop_listening)
            meeting_store.close_audio()
            try:
                # stop_listening() drains the recognizer queue.  Deliver every
                # queued transcript before acknowledging stop, so interview
                # mode can restart without losing the last spoken sentence.
                while not transcript_queue.empty():
                    message = await transcript_queue.get()
                    await websocket.send_json(message)
                if stop_requested:
                    await websocket.send_json({"type": "stopped"})
                await websocket.close()
            except Exception:
                pass


class MeetingStart(BaseModel):
    title: str = Field(default='', max_length=200)
    record_audio: bool = True


@app.post('/api/meetings')
async def start_meeting(request: MeetingStart):
    if voice_connection_lock.locked():
        raise HTTPException(409, '请先停止当前监听，再开始记录会议')
    try:
        return meeting_store.start(request.title, request.record_audio)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.get('/api/meetings')
async def list_meetings():
    return {'meetings': meeting_store.list(),
            'active_id': meeting_store.active['id'] if meeting_store.active else None}


@app.post('/api/meetings/finish')
async def finish_meeting():
    # The UI closes the voice socket first; wait for its final transcription drain.
    async with voice_connection_lock:
        try:
            return meeting_store.finish()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None


@app.post('/api/prepare-exit')
async def prepare_exit():
    await asyncio.to_thread(voice_recognizer.stop_listening)
    meeting_store.close_audio()
    if meeting_store.active:
        meeting_store.finish()
    return {'saved': True}


def read_meeting(meeting_id):
    try:
        return meeting_store.read(meeting_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, '会议记录不存在') from None


@app.get('/api/meetings/{meeting_id}')
async def get_meeting(meeting_id: str):
    return read_meeting(meeting_id)


@app.post('/api/meetings/{meeting_id}/summary')
async def summarize_meeting(meeting_id: str, live: bool = False):
    if summary_lock.locked():
        raise HTTPException(409, '总结正在生成，请稍后重试')
    async with summary_lock:
        record = read_meeting(meeting_id)
        if not live and meeting_store.active and meeting_store.active['id'] == meeting_id:
            raise HTTPException(409, '请先结束会议再生成完整总结')
        try:
            result = await llm_service.summarize_meeting(record['transcript'])
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from None
        name = 'live-summary' if live else 'summary'
        path = meeting_store.directory(meeting_id) / f'{name}.txt'
        path.write_text(result, encoding='utf-8')
        source = {'source_at': record['entries'][-1]['created_at'] if record['entries'] else None,
                  'source_characters': len(record['transcript'])}
        path.with_suffix('.json').write_text(json.dumps(source), encoding='utf-8')
        return {'summary': result, **source, 'live': live}


@app.get('/api/meetings/{meeting_id}/files/{filename}')
async def meeting_file(meeting_id: str, filename: str):
    record = read_meeting(meeting_id)
    allowed = {'transcript.txt', 'transcript.jsonl', 'summary.txt', 'live-summary.txt',
               'translations.jsonl', 'answers.jsonl'}
    allowed.update(track['file'] for track in record['tracks'])
    if filename not in allowed:
        raise HTTPException(404, '文件不存在')
    path = meeting_store.directory(meeting_id) / filename
    if not path.is_file():
        raise HTTPException(404, '文件尚未生成')
    return FileResponse(path, filename=filename)


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


@app.post("/api/translate")
async def translate_subtitle(request: TranslationRequest):
    if request.meeting_id:
        read_meeting(request.meeting_id)
    try:
        translation = await llm_service.translate_text(request.text) if request.target == 'auto' else await llm_service.translate_text(request.text, request.target)
        if request.meeting_id and request.persist:
            meeting_store.append_artifact(request.meeting_id, 'translations', {
                'original': request.text, 'translation': translation,
                'source_at': request.source_at, 'target': request.target,
            })
        return {"translation": translation}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


def _sse_event(event_type: str, **payload) -> str:
    return "data: " + json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n\n"


@app.post("/api/answer/stream")
async def generate_answer_stream(request: QuestionRequest):
    if request.meeting_id:
        read_meeting(request.meeting_id)
    async def event_generator():
        context = await knowledge_base.query(request.question) if request.use_knowledge_base else ""
        history = [message.model_dump() for message in request.conversation_history]
        try:
            answer = ''
            language_prompt = {'auto': 'Reply in the same language as the question; for mixed language use its dominant language.',
                               'zh': '请使用中文回答。', 'en': 'Reply in English.'}[request.answer_language]
            async for token in llm_service.generate_answer_stream(
                question=request.question,
                context=context,
                system_prompt=(request.system_prompt or "") + '\n' + language_prompt,
                conversation_history=history,
                temperature=1.1 if request.regenerate else 0.7,
            ):
                answer += token
                yield _sse_event("token", content=token)
                await asyncio.sleep(0)
            if not answer.strip():
                raise RuntimeError('模型没有返回答案正文')
            if request.meeting_id:
                meeting_store.append_artifact(request.meeting_id, 'answers', {
                    'question': request.question, 'answer': answer,
                    'source_at': request.source_at, 'language': request.answer_language,
                })
            yield _sse_event("done")
        except RuntimeError as exc:
            yield _sse_event("error", message=str(exc))
        except Exception:
            logger.exception('Answer generation or archive write failed')
            yield _sse_event('error', message='答案生成或保存失败，请重试；原始记录已保留')

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
        None if settings.voice_input_device == -1 else settings.voice_input_device
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
