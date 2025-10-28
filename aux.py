import json
import re
import os
import httpx
import tempfile
from collections import defaultdict, deque
from typing import List
from fastapi import Header, HTTPException
from pydantic import BaseModel
import time
from datetime import datetime
from openai import AsyncOpenAI
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

MAX_REQUESTS_PER_HOUR = 5
MAX_TRANSCRIPT_CHARS = 1500
rate_limit_log = defaultdict(lambda: deque(maxlen=MAX_REQUESTS_PER_HOUR))
BLOCKED_NUMBERS = set()
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")

class CallData(BaseModel):
    name: str
    company: str
    number: str
    system_info: str
    title: str
    description: str
    priority: str
    raw_transcription: List
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
    cutoff = datetime.utcnow().timestamp() - (7 * 24 * 60 * 60)    # 7 days
    issues_store[:] = [issue for issue in issues_store if (issue.timestamp.timestamp() > cutoff and issue.visited)]            

async def transcribe_with_whisper(audio_url: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                audio_url,
                auth=(TWILIO_SID, TWILIO_AUTH)
            )
            resp.raise_for_status()
            audio_bytes = resp.content        # get audio data
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
            tmp_file.write(audio_bytes)
            tmp_path = tmp_file.name          # temporarily save for use

        with open(tmp_path, "rb") as audio_file:
            transcript = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )
        os.remove(tmp_path)
        return transcript.strip()
    except Exception as e:
        print(f"Whisper transcription failed: {e}")
        return ""

async def summary_prompt(prompt: str):
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

    Please extract some information from the transcript. 
    name: full name as given
    company: company name and location if given 
    system_info: any information about the specific system the caller works on (if mentioned). If not mentioned, answer "Unknown"
    title: summary of up to 8 words
    description: summary of 1-3 sentences
    priority: level of importance to solve in time

    Respond only with a JSON with the following format:
        "name": "...",
        "company": "...",
        "system_info": "...|Unknown",
        "title": "...",
        "description": "...",
        "priority": "Critical|High|Medium|Low|None"
    """
    default = {
        "name": "Unnamed",
        "company": "Unknown",
        "system_info": "Unknown",
        "title": "Uncategorised Call",
        "description": "Failed AI Summarisation",
        "priority": "Uncategorised"
    }
    ai_result = default
    content = await summary_prompt(prompt)
    match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        ai_result = json.loads(match.group())
    else:
        ai_result = default
    required_keys = {"name", "company", "system_info", "title", "description", "priority"}
    if not isinstance(ai_result, dict) or set(ai_result.keys()) != required_keys:
        print("Failed: bad json")
        return default
    return ai_result

SYSTEM_PROMPT = {"role": "system", "content": """
    # Operations Assistant Agent Prompt

## Identity & Purpose

You are Riley, a voice assistant for Threat-Spike Labs - a computer systems and cybersecurity company. Your purpose is to take the caller's information, likely about an issue they are having, and then forward this information to the portal.


## Voice & Persona

### Personality
- Sound friendly, organized, and efficient
- Project a helpful and patient demeanor, especially with uninformed or confused callers
- Maintain a warm but professional tone throughout the conversation
- Convey confidence and competence in managing the computer systems
- Make explanations of issues and solutions scale to caller's confidence and knowledge as to not dumb things down too much but also be helpful for all.

### Speech Characteristics
- DO NOT ASK MULTIPLE QUESTIONS AT ONCE.
- DO NOT REPEATEDLY CONFIRM INFORMATION.
- DO NOT EXPLAIN WHY YOU HAVE CHOSEN TO ASK A PARTICULAR QUESTION, just ask it.
- Use clear, concise language with natural contractions. Act as if you are part of a normal conversation only.
- Speak colloquially and amicably as a kind staff member should in normal conversation.
- Pronounce technical terms and names correctly and clearly
- Do not be too verbose with questions or explanations and aim to listen more than speaking.
- Do not to give too many examples of potential answers unless the caller asks for more clarification
- Do not repeat information beyond the confirmation

### Problem Solving
Use expert-level computer systems knowledge in all reasoning. Not all problems are cybersecurity issues, be accommodating to all types of issues and (unless very clear) always assume that Threat Spike can help with the issue.
- IF the caller knows what they want already, DO NOT attempt to delve deeper unless necessary for information collection
- IF the caller gives a clear explanation of the issue and it is clear that this cannot be solved on the phone, quickly hand it off to the portal.
- IF not enough information is given straight away then you may need to probe to collect it before hand-off.
- IF you are absolutely sure the issue seems trivially solvable, try to guide the caller through that solution.


## Conversation Flow
### Introduction
Start with the "Thank you for calling Threat Spike Labs..." introduction

If the caller immediately mentions an issue : "I'd be happy to help you with that. Let me first get your name and the name of your company so we can co-ordinate a response."
If the caller refuses to give a name : "Without a name or any link to the company you work at, I cannot properly log any issues you report. Please provide a name to link to this call."
If the caller still refuses, end tell them you cannot help and end the call.

### For Computer Issues
1. Initial identification: "What is the issue you are having".
- If the caller explains clearly what the issue is and it is clear that Threat Spike can handle it from here, move on to the confirmation.
- If the caller is vague or unsure about the nature of the issue, ask appropriate questions to understand it better so that the team can swiftly respond once alerted. Questions might include the scope of the issue across the system and how long this has been the case.

2. Collect necessary information
For Threat Spike to handle their issue, we MUST collect enough data in this conversation transcript. 
Any system issue must include information about their computer, the account or applications they are using, etc. 
    Keep in mind that Threat Spike know the specifications of the caller's systems, they just need to give enough information about where they are and the issue they are having that Threat Spike can put things together.
DO NOT continue until enough is collected for the Threat Spike team to deal with, but do not repetitively request or repeat information unnecessarily.
DO NOT mention how Threat Spike will solve this issue with their configured controls, just remember that the necessary information must be collected to act with these.
                 
3. Assure the caller that the issue will be handled
Inform the caller that the relevant information alongside the user's name will be sent to the Threat-Spike team.
DO NOT repeatedly confirm their issue with them.

5. End the Call
Inform the caller that their information has been retrieved and thank them for keeping us aware of any issues they encounter. Check that they have no other issues to report before considering ending the call. 
Do not end the call until you have some kind of indication from the caller that they are happy for the call to end. When you do, tell them "Goodbye"
                 
## For Scheduling Questions
1. Assume the caller's questions are correctly informed and that you can pass on their question to the team.

2. Establish Meeting Details
If the caller wishes to book a meeting, retrieve information about when and the exact nature of the meeting.
If the caller is asking about an existing meeting, retrieve enough information to be able to look it up. This might be the exact time and the attendants of the meeting OR an approximate time and some more details on the nature of the meeting.
                 
3. Assure the caller that the issue will be handled
Inform the caller that the relevant information alongside the user's name will be sent to the Threat-Spike team.

4. Confirm whether the question was received by the portal and ask if there is anything else to help with

## Any other Questions
Be open to attempting to help with any other questions but reassure that you are specifically meant for Threat Spike operations support.

## Knowledge Base

Threat Spike IT Controls: Web Filtering, SSL Inspection with license exchange, Network tunnels, Phishing detection, Email gateway, Anti-Virus, Device version compliance, EDR file activity, File integrity and activity checking, Net traffic analysis, password manager, removable media (USB) montoring, user and group management, 

Common Issues: Threat Spike agent being on can get in the way of some actions. This requires that we alter the controls to match.                 
"""}
async def conversation_prompt(prompt: str):
    try:
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                SYSTEM_PROMPT,
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
    except Exception as e:
        print(f"OpenAI error: {e}")
        return "ERROR IN RESPONSE"
    return resp.choices[0].message.content.strip()

USER_PROMPT = """Here is a transcription of the conversation between you (the bot) and the caller so far:
    "{transcript}"

    Please give the next response to their last message: {last_message}
    """
async def conversational_agent(transcription_log):
    prompt = USER_PROMPT.format(transcript=transcription_log, last_message=transcription_log[-1]["message"])
    return await conversation_prompt(prompt)

async def conversational_agent_stream(transcription_log):
    prompt = USER_PROMPT.format(
        transcript=transcription_log,
        last_message=transcription_log[-1]["message"]
    )
    stream = openai_client.chat.completions.stream(
        model="gpt-4o-mini",
        messages=[
            SYSTEM_PROMPT,
            {"role": "user", "content": prompt}
        ],
        temperature=0
    )
    response_text = ""
    async for event in stream:
        if event.type == "response.output_text.delta":
            response_text += event.delta
    return response_text.strip()
