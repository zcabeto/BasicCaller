from fastapi import FastAPI, Form, Request
from fastapi.responses import Response
from pydantic import BaseModel
from typing import List
import threading
from twilio.twiml.voice_response import VoiceResponse

app = FastAPI()

class CallData(BaseModel):
    name: str
    title: str
    description: str
    priority: str
    raw_transcription: str

# thread-safe storage of issues over many calls, maintain conversation per call
store_lock = threading.Lock()
issues_store: List[CallData] = []
conversation_state = {}

@app.post("/voice")
def voice():
    """initial call start, filter urgent messages and then get name to move on with"""
    resp = VoiceResponse()
    resp.say(
        "Thank you for calling Threat Spike Labs! " \
        "If your call is urgent and you need to be handed to a member of staff, please press star. " \
        "Otherwise, please hold."
    )
    resp.gather(
        input="dtmf",
        num_digits=1,
        action="https://basic-caller.onrender.com/handle_input",
        timeout=5
    )
    resp.say(
        "Your issue has been registered as not urgent. " \
        "Before you explain this issue, please provide your first and last name."
    )
    resp.gather(
        input="speech",
        action="https://basic-caller.onrender.com/conversation",
        method="POST",
        timeout=5
    )
    resp.say("We did not receive any input. Goodbye.")
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")

@app.post("/handle_input")
def handle_input(Digits: str = Form(...)):
    """only triggers if star (*) is pressed"""
    resp = VoiceResponse()
    if Digits == "*":
        resp.say("Connecting you to a staff member now.")
        resp.hangup()
    else:
        # note for later that this hangup just sends it back so we can make a loop with the conversation_state via this
        # just have to make the timeout a bit more dynamic but this works as an initial thought
        resp.hangup()
    
    return Response(content=str(resp), media_type="text/xml")

@app.post("/conversation")
async def conversation(CallSid: str = Form(...), SpeechResult: str = Form("")):
    """pull out name and prompt for issue description"""
    resp = VoiceResponse()
    state = conversation_state.get(CallSid, {})
    
    # store name for reuse
    state['name'] = SpeechResult or "Caller"
    conversation_state[CallSid] = state
    
    # ask for issue description
    resp.say(f"Hi {state['name']}, please describe your issue after the beep.")
    resp.record(
        transcribe=True,
        transcribe_callback="https://basic-caller.onrender.com/transcription",
        max_length=120,
        play_beep=True
    )
    resp.hangup()
    
    return Response(content=str(resp), media_type="text/xml")

@app.post("/transcription")
async def transcription(CallSid: str = Form(...), From: str = Form("Unknown"), TranscriptionText: str = Form("")):
    """create transcription and store the issue"""
    state = conversation_state.get(CallSid, {})
    
    issue = CallData(
        name=state.get('name', "(empty)"),
        number=From,
        title="Inbound Phone Call",
        description=TranscriptionText or "(empty)",
        priority="medium",
        raw_transcription=TranscriptionText or "(empty)"
    )
    
    with store_lock:
        issues_store.append(issue)
    
    return {"status": "saved"}

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
