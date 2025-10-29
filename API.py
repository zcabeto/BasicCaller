import os
from fastapi import FastAPI, Form, Request, Depends
from fastapi.responses import Response, JSONResponse, FileResponse
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from typing import List
from datetime import datetime
import threading
import re
from uuid import uuid4
from aux import CallData, is_blocked, is_e164, is_rate_limited, log_request, clear_old_issues, generate_summary, verify_api_key, SYSTEM_PROMPT, openai_client

TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
app = FastAPI()
store_lock = threading.Lock()
issues_store: List[CallData] = []
conversation_state = {}

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "message": "FastAPI + Twilio server is running"}

validator = RequestValidator(TWILIO_AUTH)
@app.middleware("http")
async def verify_twilio_signature(request: Request, call_next):
    """POST requests only made by Twilio"""
    if request.url.path.startswith("/poll") or request.url.path == "/":
        return await call_next(request)    # anyone can poll

    twilio_signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    body = await request.body()
    form_data = dict(await request.form()) if request.method == "POST" else {}

    if not validator.validate(url, form_data, twilio_signature):    # validate POSTs
        return JSONResponse(
            status_code=403,
            content={"detail": "Invalid Twilio signature"}
        )
    return await call_next(request)

@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    file_path = f"/tmp/{filename}"
    if not os.path.exists(file_path):
        return Response(status_code=404)
    return FileResponse(file_path, media_type="audio/mpeg")

async def speak(resp, text: str):
    tts_resp = await openai_client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input=text
    )
    audio_bytes = tts_resp.read()
    file_id = f"{uuid4()}.mp3"
    file_path = f"/tmp/{file_id}"
    with open(file_path, "wb") as f:
        f.write(audio_bytes)
    audio_url = f"https://autoreceptionist.onrender.com/audio/{file_id}"
    resp.play(audio_url)

@app.post("/voice")
async def start_call(CallSid: str = Form(...), From: str = Form("Unknown", alias="From")):
    """initial call start, filter urgent messages and then get name to move on with"""
    resp = VoiceResponse()
    with store_lock:
        if not is_e164(From):        # incorrect format can infer a spoofed number
            print(f"Invalid caller: {From}")
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/blocked.mp3")
            resp.hangup()
        if is_rate_limited(From):    # limit callers calling too many times per hour
            print(f"Rate-limited caller: {From}")
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/max_calls.mp3")
            resp.hangup()
            return Response(content=str(resp), media_type="text/xml")
        log_request(From)
        if is_blocked(From):  # known malicious numbers
            print(f"Blocked caller: {From}")
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/blocked.mp3")
            resp.hangup()
            return Response(content=str(resp), media_type="text/xml")
        # every time a call is initiated, refresh the stored issues
        clear_old_issues(issues_store)
        state = conversation_state.get(CallSid, {})
        state['raw_transcript'] = [{"role": "bot", "message": "Thank you for calling Threat Spike Labs. This is Riley, your operations assistant. Just to let you know you can press STAR at any time to register this as an urgent call and speak to our team. With that out the way, how may I help you today?"}]
        conversation_state[CallSid] = state

    await speak(resp,"Thank you for calling Threat Spike Labs. This is Riley, your operations assistant. Just to let you know you can press STAR at any time to register this as an urgent call and speak to our team. With that out the way, how may I help you today?")
    resp.gather(
        input="speech",
        action="https://autoreceptionist.onrender.com/conversation",
        method="POST",
        timeout=2
    )
    return Response(content=str(resp), media_type="text/xml")

def handle_urgent(Digits: str = ""):
    resp = VoiceResponse()
    if Digits == "*":
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/transfer_call.mp3")
        dial = resp.dial(caller_id="+447367616944")
        dial.number("+447873665370")
        return Response(content=str(resp), media_type="text/xml")
    return None

USER_PROMPT = """
Here is a transcription of the conversation between you (the bot) and the caller so far:
    "{transcript}"

    Please give the next response (ONLY ONE OR TWO SENTENCES) to their last message: {last_message}
"""

@app.post("/conversation")
async def conversation(request: Request, Digits: str = Form(""), CallSid: str = Form(...)):
    urgent_response = handle_urgent(Digits)
    if urgent_response:
        return urgent_response

    form = await request.form()
    user_input = form.get("SpeechResult", "")
    with store_lock:
        state = conversation_state.get(CallSid, {})
        state['raw_transcript'].append({"role":"caller", "message":user_input})
        user_prompt = USER_PROMPT.format(transcript=state['raw_transcript'], last_message=user_input)
        conversation_state[CallSid] = state
    print(f"Caller said: {user_input}")
    response_text = ""
    first_chunk = None
    max_sentences = 2
    async with openai_client.chat.completions.stream(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    ) as stream:
        async for event in stream:
            if event.type == "content.delta":
                response_text += event.delta
                matches = re.findall(r"[.!?]\s", response_text)
                if len(matches) >= max_sentences:
                    split_point = 0
                    count = 0
                    for m in re.finditer(r"[.!?]\s", response_text):
                        count += 1
                        if count == max_sentences:
                            split_point = m.end()
                            break
                    first_chunk = response_text[:split_point].strip()
                    break
                else:
                    first_chunk = response_text.strip()
    with store_lock:
        state = conversation_state.get(CallSid, {})
        state['raw_transcript'].append({"role":"bot", "message":first_chunk})
        conversation_state[CallSid] = state
    resp = VoiceResponse()
    if first_chunk:
        await speak(resp,first_chunk)
    else:
        await speak(resp,response_text.strip() or "Sorry, I didn't catch that.")
    if "goodbye" in first_chunk.lower():
        return Response(content=str(resp), media_type="text/xml")
    resp.gather(
        input="speech dtmf",
        action="https://autoreceptionist.onrender.com/conversation",
        method="POST",
        timeout=2
    )
    print("Sending partial TwiML:\n", str(resp))
    return Response(content=str(resp), media_type="text/xml")

@app.post("/end_call")
async def get_issue_type(CallSid: str = Form(...), From: str = Form("Unknown", alias="From"), CallStatus: str = Form("")):
    with store_lock:
        print(f"Call {CallSid} ended with status {CallStatus}")
        state = conversation_state.get(CallSid, {})
        summary = await generate_summary(state['raw_transcript'])
        raw_transcript = [ message["message"] for message in state['raw_transcript'] ]
        state['issue'] = CallData(
            name=summary.get('name', From),
            company=summary.get('company', 'no company information'),
            number=state.get('number', From),
            system_info=summary.get('system_info', "no device information"),
            title=summary['title'],
            description=summary['description'],
            priority=summary['priority'],
            raw_transcription=raw_transcript,
            visited=False,
            timestamp=datetime.utcnow()
        )
        issues_store.append(state['issue'])
        return {"status": "saved"}

@app.get("/poll/")
def poll(authorized: bool = Depends(verify_api_key)):
    with store_lock:
        issues_out = issues_store.copy()
        for issue in issues_store:
            issue.visited=True
        clear_old_issues(issues_store)
        return {"issues": issues_out}
