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
def start_call(From: str = Form("Unknown", alias="From")):
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
        
    urgency_gather = resp.gather(
        input="dtmf",
        num_digits=1,
        action="https://autoreceptionist.onrender.com/urgent_call",
        timeout=3
    )
    urgency_gather.play("https://zcabeto.github.io/BasicCaller-Audios/audios/start-call.mp3")
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/not_urgent.mp3")
    resp.redirect("https://autoreceptionist.onrender.com/ask_name")
    return Response(content=str(resp), media_type="text/xml")

@app.post("/urgent_call")
def urgent_call(Digits: str = Form(...)):
    """only triggers if star (*) is pressed"""
    resp = VoiceResponse()
    if Digits == "*":
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/transfer_call.mp3")
        dial = resp.dial(caller_id="+447367616944")
        dial.number("+447873665370")
    else:
        resp.hangup()
    
    return Response(content=str(resp), media_type="text/xml")

@app.post("/ask_name")
def ask_name():
    """ask name of caller for log matching"""
    resp = VoiceResponse()
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/ask_name.mp3")
    resp.record(
        input="speech",
        action="https://autoreceptionist.onrender.com/get_type",
        method="POST",
        max_length=5,
        trim="trim-silence",
        play_beep=False
    )
    
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/no_input.mp3")
    return Response(content=str(resp), media_type="text/xml")

@app.post("/get_type")
async def get_issue(CallSid: str = Form(...), RecordingUrl: str = Form(""), From: str = Form("Unknown", alias="From")):
    """Ask the caller to pick what type of issue they have"""
    resp = VoiceResponse()
    print("entered request issue")
    with store_lock:
        state = conversation_state.get(CallSid, {})
        state['number'] = From
        whisper_text = await transcribe_with_whisper(f"{RecordingUrl}.wav") if RecordingUrl else ""
        state['name'] = whisper_text or ""
        state['name'] = ''.join(char for char in state['name'] if char.isalnum() or char==' ')    # clean: only letters
        if len(state['name'].split()) < 3:
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/no_input.mp3")
            resp.redirect("https://autoreceptionist.onrender.com/ask_name")
        
        state['raw_transcript'] = [{"role": "bot", "message": "Hi there, thank you for calling ThreatSpike Labs. If your call is urgent and you need to speak to a member of staff, please press star"}, {"role": "bot", "message": "We've registered your call as not urgent. Before we start, could you provide your full name and the name of your company?"}]
        state['raw_transcript'].append({"role": "caller", "message": state['name']})
        state['raw_transcript'].append({"role": "bot", "message": "Alright, thank you. Now tell me about your issue."})
        conversation_state[CallSid] = state
    print("asking issue now")
    resp.say("Alright, thank you. Now tell me about your issue.")
    resp.gather(
        input="speech",
        action="https://autoreceptionist.onrender.com/conversation",
        method="POST",
        timeout=3
    )
    return Response(content=str(resp), media_type="text/xml")


@app.post("/conversation")
async def get_issue_type(CallSid: str = Form(...), SpeechResult: str = Form("")):
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
        state['raw_transcript'].append({"role": "bot", "message": bot_answer})
        conversation_state[CallSid] = state
        resp.say(bot_answer)
        resp.gather(
            input="speech",
            action="https://autoreceptionist.onrender.com/conversation",
            method="POST",
            status_callback="https://autoreceptionist.onrender.com/end_call",
            status_callback_event=["completed"]
            timeout=2
        )
    return Response(content=str(resp), media_type="text/xml")

@app.post("/end_call")
async def get_issue_type(CallSid: str = Form(...), From: str = Form("Unknown", alias="From")):
    with store_lock:
        state = conversation_state.get(CallSid, {})
        summary = await generate_summary(state['raw_transcription'])
        raw_transcript = [ message["message"] for message in state['raw_transcript'] ]
        state['issue'] = CallData(
            name=state.get('name', "Caller"),
            number=state.get('number', From),
            system_info=state.get('system_info', "no device information"),
            issue_type=state.get('issue_type', 'unknown'),
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
