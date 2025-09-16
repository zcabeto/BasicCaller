import json
import re
import os
from collections import defaultdict, deque
from fastapi import Header, HTTPException
from pydantic import BaseModel
import time
from datetime import datetime
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
    visited: bool
    timestamp: datetime = datetime.utcnow() 

API_KEY = os.getenv("POLL_API_KEY")
def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

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

def clear_old_issues(issues_store):
    cutoff = datetime.utcnow().timestamp() - (1 * 1 * 15 * 60)    # 15 mins
    issues_store[:] = [issue for issue in issues_store if (issue.timestamp.timestamp() > cutoff and issue.visited)]            

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
        "description": "Failed AI Summarisation",
        "priority": "unknown"
    }
    ai_result = default
    content = await execute_prompt(prompt)
    match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        ai_result = json.loads(match.group())
    else:
        ai_result = default
        print("RawContent:", resp(content))
    required_keys = {"title", "description", "priority"}
    if not isinstance(ai_result, dict) or set(ai_result.keys()) != required_keys:
        print("Failed: bad json")
        return default
    return ai_result
