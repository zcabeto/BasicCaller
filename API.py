import os
from fastapi import FastAPI, Form, BackgroundTasks, Query, Request, Depends
from fastapi.responses import Response, JSONResponse
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from typing import List
from datetime import datetime
import threading
from aux import CallData, is_blocked, is_e164, is_rate_limited, log_request, clear_old_issues, generate_summary, verify_api_key

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

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
def start_call(From: str = Form("Unknown")):
    """initial call start, filter urgent messages and then get name to move on with"""
    resp = VoiceResponse()
    with store_lock:
        if not is_e164(From):        # incorrect format can infer a spoofed number
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/blocked.mp3")
            resp.hangup()
        if is_rate_limited(From):    # limit callers calling too many times per hour
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/max_calls.mp3")
            resp.hangup()
            return Response(content=str(resp), media_type="text/xml")
        log_request(From)
        if is_blocked(From):  # known malicious numbers
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/blocked.mp3")
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
    urgency_gather.play("https://zcabeto.github.io/BasicCaller-Audios/urgent_call.mp3")
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/not_urgent.mp3")
    resp.redirect("https://autoreceptionist.onrender.com/ask_name")
    return Response(content=str(resp), media_type="text/xml")

@app.post("/urgent_call")
def urgent_call(Digits: str = Form(...)):
    """only triggers if star (*) is pressed"""
    resp = VoiceResponse()
    if Digits == "*":
        #resp.say("Connecting you to a staff member now.")
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/transfer_call.mp3")
        dial = resp.dial(caller_id="+447367616944")
        dial.number("+447873665370")
    else:
        resp.hangup()
    
    return Response(content=str(resp), media_type="text/xml")

@app.post("/ask_name")
def ask_name():
    """ask name of caller for log matching"""
    resp = VoiceResponse()
    name_gather = resp.gather(
        input="speech",
        action="https://autoreceptionist.onrender.com/issue_type",
        method="POST",
        timeout=3
    )
    name_gather.play("https://zcabeto.github.io/BasicCaller-Audios/give_name.mp3") # split not urgent & ask name
    
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/no_input.mp3")
    return Response(content=str(resp), media_type="text/xml")

@app.post("/issue_type")
def get_issue_type(CallSid: str = Form(...), SpeechResult: str = Form(""), From: str = Form("Unknown")):
    """Ask the caller to pick what type of issue they have"""
    resp = VoiceResponse()

    with store_lock:
        state = conversation_state.get(CallSid, {})
        state['number'] = From
        state['name'] = SpeechResult if SpeechResult else state.get('name', "Caller")
        state['name'] = ''.join(char for char in state['name'] if char.isalnum() or char==' ')    # clean: only letters
        if len(state['name'].split()) < 2:
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/not_enough.mp3")
            resp.redirect("https://autoreceptionist.onrender.com/ask_name")
        conversation_state[CallSid] = state

    issue_gather = resp.gather(
        input="dtmf",
        num_digits=1,
        action="https://autoreceptionist.onrender.com/issue_resolve",
        timeout=5
    )
    issue_gather.play("https://zcabeto.github.io/BasicCaller-Audios/query_option.mp3")
    return Response(content=str(resp), media_type="text/xml")

@app.post("/issue_resolve")
def issue_resolve(Digits: str = Form(""), CallSid: str = Form(...)):
    resp = VoiceResponse()

    if Digits:
        with store_lock:
            state = conversation_state.get(CallSid, {})
            state['issue_type'] = "systems" if Digits == "1" else (
                "scheduling" if Digits == "2" else ("general" if Digits == "3" else "unknown"))
            conversation_state[CallSid] = state

    state = conversation_state.get(CallSid, {})

    if state.get("issue_type") == "systems":
        system_gather = resp.gather(
            input="speech",
            action="https://autoreceptionist.onrender.com/explain_issue",
            method="POST",
            timeout=3
        )
        system_gather.play("https://zcabeto.github.io/BasicCaller-Audios/sys_info.mp3")
        
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/no_input.mp3")
        resp.redirect("https://autoreceptionist.onrender.com/issue_resolve")
        resp.hangup()
    elif state.get("issue_type") in ["scheduling", "general"]:
        resp.redirect("/explain_issue")
    else:    # "unknown" issue i.e. invalid number entered
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/invalid.mp3")
        resp.redirect("/issue_type")

    return Response(content=str(resp), media_type="text/xml")

@app.post("/explain_issue")
async def explain_issue(CallSid: str = Form(...), SpeechResult: str = Form(""), From: str = Form("Unknown")):
    """Handle system info transcription and ask for main issue description"""
    resp = VoiceResponse()
    with store_lock:
        state = conversation_state.get(CallSid, {})
        if state.get("issue_type") == "systems":
            if state.get("issue_type") == "systems" and SpeechResult:
                state['system_info'] = SpeechResult
                if len(state['system_info'].split()) < 3:
                    resp.play("https://zcabeto.github.io/BasicCaller-Audios/no_input.mp3")    # "sorry, I didn't catch that" then loop
                    resp.redirect("https://autoreceptionist.onrender.com/issue_resolve")
                conversation_state[CallSid] = state

    # Now ask for main issue description
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/ask_issue.mp3")
    resp.record(
        transcribe=True,
        transcribe_callback="https://autoreceptionist.onrender.com/transcription",
        action="https://autoreceptionist.onrender.com/timeout",    # end of 120s
        max_length=120,
        play_beep=True
    )
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")

@app.post("/transcription")
async def transcription(CallSid: str = Form(...), From: str = Form("Unknown"), TranscriptionText: str = Form(""), background_tasks: BackgroundTasks = None, Direction: str = Form("inbound"), overwritten_issue_SID: str = Query(None)):
    """create transcription and store the issue"""
    with store_lock:
        state = conversation_state.get(CallSid, {})

        summary = {
            "title": "Uncategorized Call",
            "description": TranscriptionText,
            "priority": "unknown"
        }
        summary = await generate_summary(TranscriptionText)
        
        state['issue'] = CallData(
            name=state.get('name', "Caller"),
            number=state.get('number', From),
            system_info=state.get('system_info', "no device information"),
            issue_type=state.get('issue_type', 'system'),
            title=summary['title'],
            description=summary['description'],
            priority=summary['priority'],
            raw_transcription=(TranscriptionText or "(empty)"),
            visited=False,
            timestamp=datetime.utcnow()
        )
        issues_store.append(state['issue'])
        return {"status": "saved"}

@app.post("/timeout")
async def timeout(RecordingDuration: str = Form("")):
    try:
        duration = int(RecordingDuration or 0)
    except ValueError:
        duration = 0
    resp = VoiceResponse()
    if duration > 110:  # hit max length
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/goodbye.mp3")
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")


@app.get("/poll/")
def poll(authorized: bool = Depends(verify_api_key)):
    with store_lock:
        issues_out = issues_store.copy()
        for issue in issues_store:
            issue.visited=True
        clear_old_issues(issues_store)
        return {"issues": issues_out}
