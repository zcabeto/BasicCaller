from fastapi import FastAPI, Form, BackgroundTasks, Query
from fastapi.responses import Response
from pydantic import BaseModel
from typing import List
import threading
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import os
from urllib.parse import quote_plus, unquote_plus
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

app = FastAPI()

class CallData(BaseModel):
    name: str
    number: str
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
        dial = resp.dial(caller_id="+447367616944")
        dial.number("+447873665370")
    else:
        # note for later that this hangup just sends it back so we can make a loop with the conversation_state via this
        # just have to make the timeout a bit more dynamic but this works as an initial thought
        resp.hangup()
    
    return Response(content=str(resp), media_type="text/xml")

@app.post("/conversation")
async def conversation(CallSid: str = Form(...), SpeechResult: str = Form(""), From: str = Form("Unknown")):
    """pull out name and prompt for issue description"""
    resp = VoiceResponse()

    with store_lock:
        state = conversation_state.get(CallSid, {})
        # always store number & number
        state['number'] = From
        state['name'] = SpeechResult if SpeechResult else state.get('name', "Caller")
        conversation_state[CallSid] = state

    # ask for issue description
    resp.say(f"Hi {state['name']}, please describe your issue after the beep. Once you are done, please hang up and we will call you back shortly to authenticate your number and check a summary of the issue you described.")
    resp.record(
        transcribe=True,
        transcribe_callback="https://basic-caller.onrender.com/transcription",
        max_length=120,
        play_beep=True
    )
    resp.hangup()
    
    return Response(content=str(resp), media_type="text/xml")


@app.post("/transcription")
async def transcription(CallSid: str = Form(...), From: str = Form("Unknown"), TranscriptionText: str = Form(""), background_tasks: BackgroundTasks = None, Direction: str = Form("inbound"), overwritten_issue_SID: str = Query(None)):
    """create transcription and store the issue"""
    with store_lock:
        if not overwritten_issue_SID:    # initial call
            state = conversation_state.get(CallSid, {})
    
            summary = {
                "title": "Uncategorized Call",
                "description": TranscriptionText,
                "priority": "unknown"
            }
            # summary = generate_summary(TranscriptionText)
            
            state['pending_issue'] = CallData(
                name=state.get('name', "Caller"),
                number=state.get('number', From),
                title=summary['title'],
                description=summary['description'],
                priority=summary['priority'],
                raw_transcription=(TranscriptionText or "(empty)")
            )
            conversation_state[CallSid] = state
        else:    # transcription of second-try call
            state = conversation_state(overwritten_issue_SID)
            assert "pending_issue" in state
            issues_store.append(state['pending_issue'])
            conversation_state.pop(overwritten_issue_SID, None)

    if TWILIO_NUMBER and From and Direction == "inbound":
        def initiate_callback():
            twilio_client.calls.create(
                to=From,
                from_=TWILIO_NUMBER,
                url=(
                    f"https://basic-caller.onrender.com/callback_summary"
                    f"?caller={quote_plus(state['pending_issue'].name)}"
                    f"&desc={quote_plus(state['pending_issue'].description)}"
                    f"&original_SID={CallSid}"
                )
            )
        background_tasks.add_task(initiate_callback)
    return {"status": "saved"}

@app.post("/callback_summary")
async def callback_summary(original_SID: str = Query(...), caller: str = "", desc: str = ""):
    """Twilio fetches this when the user answers the callback"""
    resp = VoiceResponse()
    resp.say(
        f"Hello {caller}. We are calling you back to authenticate a call you recently placed registering an issue. <break time='2s'/>"
        f"We recorded your issue as the following: <break time='1s'/> {desc}. "
        "If this you made this call, please press 1. <break time='1s'/> If this summary is incorrect, press 2. <break time='1s'/>"
        "If you did not call us, press 3 to reject this call entirely."
    )
    resp.gather(
        input="dtmf",
        num_digits=1,
        action=f"https://basic-caller.onrender.com/confirm_issue?original_SID={original_SID}",
        method="POST",
        timeout=5
    )
    resp.say("We did not receive any input. Goodbye.")
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")

@app.post("/confirm_issue")
async def confirm_issue(original_SID: str = Query(...), Digits: str = Form(...)):
    """Confirm or reject the recorded issue"""
    resp = VoiceResponse()
    with store_lock:
        state = conversation_state.get(original_SID, {})
        pending_issue = state.get("pending_issue")
    if not pending_issue:
        resp.say("We could not find your issue. Goodbye.")
        resp.hangup()
        return Response(content=str(resp), media_type="text/xml")

    if Digits == "1":           # accept issue
        with store_lock:
            issues_store.append(pending_issue)
            conversation_state.pop(original_SID, None)
        resp.say("Thank you. Your issue has been recorded.")
        resp.hangup()
    elif Digits == "2":       # retry description
        resp.say("Okay, please describe your issue again after the beep.")
        resp.record(
            transcribe=True,
            transcribe_callback="https://basic-caller.onrender.com/transcription",
            max_length=120,
            play_beep=True
        )
        resp.hangup()
    elif Digits == "3":        # discard
        with store_lock:
            conversation_state.pop(original_SID, None)
        resp.say("Your previous issue has been discarded. Goodbye.")
        resp.hangup()
    else:                      # wrong number entered
        resp.say("Invalid input. Press 1 to accept, 2 to re-record, or 3 to reject.")
        resp.gather(
            input="dtmf",
            num_digits=1,
            action=f"https://basic-caller.onrender.com/confirm_issue?original_SID={original_SID}",
            method="POST",
            timeout=5
        )
        resp.say("We did not receive any input. Goodbye.")
        resp.hangup()
    return Response(content=str(resp), media_type="text/xml")

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
