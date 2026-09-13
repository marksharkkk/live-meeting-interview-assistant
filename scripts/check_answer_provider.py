"""Small live diagnostic; prints only lengths and finish status, never credentials."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.llm_service import LLMService
from backend.config import settings


async def main():
    service = LLMService()
    try:
        options = {} if '--baseline' in sys.argv else service._answer_options()
        stream = await service.client.chat.completions.create(
            model=settings.openai_model,
            messages=[{'role': 'user', 'content': 'In two sentences, explain how you learn a new skill.'}],
            max_tokens=1000, stream=True, **options,
        )
        visible = reasoning = 0
        finish = None
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            visible += len(choice.delta.content or '')
            reasoning += len(getattr(choice.delta, 'reasoning_content', '') or '')
            finish = choice.finish_reason or finish
        print({'visible_characters': visible, 'reasoning_characters': reasoning, 'finish': finish})
        if not visible:
            raise SystemExit(1)
    finally:
        await service.client.close()


asyncio.run(asyncio.wait_for(main(), timeout=60))
