from fastapi import FastAPI, Form, BackgroundTasks, Query, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import List
import threading
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from twilio.request_validator import RequestValidator
import os
from openai import AsyncOpenAI
from urllib.parse import quote_plus, unquote_plus
import json
import re

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = FastAPI()
validator = RequestValidator(TWILIO_AUTH)

@app.middleware("http")
async def verify_twilio_signature(request: Request, call_next):
    """POST requests only made by Twilio"""
    if request.url.path.startswith("/poll"):
        return await call_next(request)    # anyone can poll

    twilio_signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    body = await request.body()
    form_data = dict(await request.form()) if request.method == "POST" else {}

    if not validator.validate(url, form_data, twilio_signature):    # validate POSTs
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    return await call_next(request)

class CallData(BaseModel):
    name: str
    number: str
    system_info: str
    issue_type: str
    title: str
    description: str
    priority: str
    raw_transcription: str

# thread-safe storage of issues over many calls, maintain conversation per call
store_lock = threading.Lock()
issues_store: List[CallData] = []
conversation_state = {}

E164_REGEX = re.compile(r'^\+[1-9]\d{1,14}$')
def is_e164(number: str) -> bool:
    print("Number:",number)
    return bool(E164_REGEX.match(number))

@app.post("/voice")
def voice(From: str = Form("Unknown")):
    """initial call start, filter urgent messages and then get name to move on with"""
    resp = VoiceResponse()
    if not is_e164(From):
        resp.say("Number is invalid")
        resp.hangup()
    urgency_gather = resp.gather(
        input="dtmf",
        num_digits=1,
        action="https://basic-caller.onrender.com/urgent_call",
        timeout=3
    )
    urgency_gather.play("https://zcabeto.github.io/BasicCaller-Audios/audios/urgent_call.mp3")
    resp.redirect("https://basic-caller.onrender.com/ask_name")
    return Response(content=str(resp), media_type="text/xml")

@app.post("/ask_name")
def ask_name():
    """ask name of caller for log matching"""
    resp = VoiceResponse()
    name_gather = resp.gather(
        input="speech",
        action="https://basic-caller.onrender.com/issue_type",
        method="POST",
        timeout=3
    )
    name_gather.play("https://zcabeto.github.io/BasicCaller-Audios/audios/ask_name.mp3") # split not urgent & ask name
    
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/no_input.mp3")
    resp.redirect("https://basic-caller.onrender.com/ask_name")
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")

@app.post("/urgent_call")
def urgent_call(Digits: str = Form(...)):
    """only triggers if star (*) is pressed"""
    resp = VoiceResponse()
    if Digits == "*":
        #resp.say("Connecting you to a staff member now.")
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/transfer_call.mp3")
        dial = resp.dial(caller_id="+447367616944")
        dial.number("+447873665370")
    else:
        # note for later that this hangup just sends it back so we can make a loop with the conversation_state via this
        # just have to make the timeout a bit more dynamic but this works as an initial thought
        resp.hangup()
    
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
            resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/no_input.mp3")    # "sorry, I didn't catch that" then loop
            resp.redirect("https://basic-caller.onrender.com/ask_name")
        conversation_state[CallSid] = state

    issue_gather = resp.gather(
        input="dtmf",
        num_digits=1,
        action="https://basic-caller.onrender.com/issue_resolve",
        timeout=5
    )
    issue_gather.say(
        "<speak>"
        "For computer or security issues, press 1. "
        "<break time='0.3s'/> For scheduling issues, press 2. "
        "<break time='0.3s'/> For general queries, press 3."
        "</speak>"
    )
    return Response(content=str(resp), media_type="text/xml")


@app.post("/issue_resolve")
def issue_resolve(Digits: str = Form(""), CallSid: str = Form(...)):
    resp = VoiceResponse()

    if Digits:
        with store_lock:
            state = conversation_state.get(CallSid, {})
            state['issue_type'] = "systems" if Digits == "1" else ("scheduling" if Digits == "2" else "general")
            conversation_state[CallSid] = state

    state = conversation_state.get(CallSid, {})

    if state.get("issue_type") == "systems":
        system_gather = resp.gather(
            input="speech",
            action="https://basic-caller.onrender.com/explain_issue",
            method="POST",
            timeout=3
        )
        system_gather.play("https://zcabeto.github.io/BasicCaller-Audios/audios/system_info.mp3")
        
        resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/no_input.mp3")
        resp.redirect("https://basic-caller.onrender.com/issue_resolve")
        resp.hangup()
    elif state.get("issue_type") in ["scheduling", "general"]:
        resp.redirect("/explain_issue")
    else:
        # Only for truly invalid DTMF input
        resp.say("Invalid input. Press 1 for computer issues, 2 for scheduling, or 3 for general queries.")
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
                    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/no_input.mp3")    # "sorry, I didn't catch that" then loop
                    resp.redirect("https://basic-caller.onrender.com/issue_resolve")
                conversation_state[CallSid] = state

    # Now ask for main issue description
    resp.play("https://zcabeto.github.io/BasicCaller-Audios/audios/explain_issue.mp3")
    resp.record(
        transcribe=True,
        transcribe_callback="https://basic-caller.onrender.com/transcription",
        max_length=120,
        play_beep=True
    )
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")


## GENERATE SUMMARY OF TRANSCRIPTION
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

async def generate_summary(transcription_text: CallData):
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

## CREATE AND CHECK TRANSCRIPTION
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
        #summary = await generate_summary(TranscriptionText)
        
        state['issue'] = CallData(
            name=state.get('name', "Caller"),
            number=state.get('number', From),
            system_info=state.get('system_info', "no device information"),
            issue_type=state.get('issue_type', 'system'),
            title=summary['title'],
            description=summary['description'],
            priority=summary['priority'],
            raw_transcription=(TranscriptionText or "(empty)")
        )
        issues_store.append(state['issue'])
        return {"status": "saved"}


## ALLOW PULL FROM SERVER
@app.get("/poll/")
def poll():
    with store_lock:
        return {"issues": issues_store}

@app.get("/poll_and_clear/")
def poll_and_clear():
    with store_lock:
        issues = issues_store.copy()
        issues_store.clear()
    return {"issues": issues}
