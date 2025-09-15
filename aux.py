import json
import re
import os
from collections import defaultdict, deque
from pydantic import BaseModel
import time
from openai import AsyncOpenAI
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

MAX_REQUESTS_PER_HOUR = 3
MAX_TRANSCRIPT_CHARS = 1500
rate_limit_log = defaultdict(lambda: deque(maxlen=MAX_REQUESTS_PER_HOUR))
BLOCKED_NUMBERS = set()

class CallData(BaseModel):
    name: str
    number: str
    system_info: str
    issue_type: str
    title: str
    description: str
    priority: str
    raw_transcription: str

def is_rate_limited(number: str) -> bool:
    """check if a number exceeded hourly calls limit"""
    now = time.time()
    window_start = now - 3600  # 1 hour ago
    timestamps = rate_limit_log[number]

    while timestamps and timestamps[0] < window_start:
        timestamps.popleft()    # refresh timestamps
    return len(timestamps) >= MAX_REQUESTS_PER_HOUR

def log_request(number: str):
    rate_limit_log[number].append(time.time())

E164_REGEX = re.compile(r'^\+[1-9]\d{1,14}$')
def is_e164(number: str) -> bool:
    return bool(E164_REGEX.match(number))

def is_blocked(number: str) -> bool:
    return (number in BLOCKED_NUMBERS)

async def execute_prompt(prompt: str):
    try:
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an assistant that extracts structured information from call transcripts."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
    except Exception as e:
        print(f"OpenAI error: {e}")
        return "fail"
    return resp.choices[0].message.content.strip()

async def generate_summary(transcription_text: str):
    transcription_text = transcription_text[:MAX_TRANSCRIPT_CHARS]
    prompt = f"""You are logging customer support phone calls. The customer has called and explained an issue.
    Caller transcription:
    "{transcription_text}"

    Please extract a short descriptive title (up to 8 words), a longer summary (1-3 sentences), and a priority level.
    Respond only with a JSON with the following format: 
        "title": "...",
        "description": "...",
        "priority": "urgent|high|medium|low|none"
    """
    default = {
        "title": "Uncategorized Call",
        "description": transcription_text,
        "priority": "unknown"
    }
    ai_result = default
    content = await execute_prompt(prompt)
    try:
        ai_output = json.loads(content)
        ai_result = ai_output
    except json.JSONDecodeError:
        ai_result = default
    required_keys = {"title", "description", "priority"}
    if not isinstance(ai_output, dict) or set(ai_output.keys()) != required_keys:
        return default
    return ai_result
