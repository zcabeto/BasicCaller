import os
from fastapi import FastAPI, Form, BackgroundTasks, Query, Request, Depends
from fastapi.responses import Response, JSONResponse
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from typing import List
from datetime import datetime
import threading
from aux import CallData, is_blocked, is_e164, is_rate_limited, log_request, clear_old_issues, generate_summary, transcribe_with_whisper, verify_api_key, conversational_agent


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

@app.post("/voice")
def start_call(CallSid: str = Form(...), From: str = Form("Unknown", alias="From")):
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

    resp.say("Thank you for calling Threat Spike Labs. This is Riley, your operations assistant. Just to let you know you can press STAR at any time to register this as an urgent call and speak to our team. With that out the way, how may I help you today?")
    resp.gather(
        input="speech",
        action="https://autoreceptionist.onrender.com/conversation",
        method="POST",
        timeout=1.5
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

@app.post("/conversation")
async def get_issue_type(CallSid: str = Form(...), SpeechResult: str = Form(""), Digits: str = Form("")):
    urgent_response = handle_urgent(Digits)
    if urgent_response:
        return urgent_response

    resp = VoiceResponse()
    with store_lock:
        state = conversation_state.get(CallSid, {})
        if not SpeechResult:
            resp.say(state['raw_transcript'][-1]["message"])
            resp.redirect("https://autoreceptionist.onrender.com/conversation")
        caller_speech = ''.join(char for char in SpeechResult if char.isalnum() or char==' ')    # clean: only letters
        if len(caller_speech.split()) < 3:
            resp.say(state['raw_transcript'][-1]["message"])
            resp.redirect("https://autoreceptionist.onrender.com/conversation")
        state['raw_transcript'].append({"role": "caller", "message": caller_speech})
        bot_answer = await conversational_agent(state['raw_transcript'])
        if bot_answer == "ERROR IN RESPONSE":
            resp.say("Error encountered. Goodbye")
            return Response(content=str(resp), media_type="text/xml")
        if "goodbye" in bot_answer.lower():
            return Response(content=str(resp), media_type="text/xml")
        state['raw_transcript'].append({"role": "bot", "message": bot_answer})
        conversation_state[CallSid] = state
        resp.say(bot_answer)
        resp.gather(
            input="dtmf speech",
            action="https://autoreceptionist.onrender.com/conversation",
            method="POST",
            status_callback="https://autoreceptionist.onrender.com/end_call",
            status_callback_event=["completed"],
            timeout=1.5
        )
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
