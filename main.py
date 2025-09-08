from fastapi import FastAPI, Form
from fastapi.responses import Response
from pydantic import BaseModel
from typing import List
import threading
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

app = FastAPI()

class CallData(BaseModel):
    name: str
    title: str
    description: str
    priority: str
    raw_transcription: str

# thread-safe in-memory store
store_lock = threading.Lock()
issues_store: List[CallData] = []
log_store = []

# Twilio webhook: what to do when call is answered
@app.post("/voice")
def voice():
    resp = VoiceResponse()
    resp.say("Thank you for calling Threat Spike Labs. Leave your name and any issues you wish to talk about after the beep.")
    resp.record(
        transcribe=True,
        transcribe_callback="https://basic-caller.onrender.com/transcription",
        max_length=120,
        play_beep=True
    )
    resp.hangup()
    return Response(content=str(resp), media_type="text/xml")

# Twilio webhook: transcription result
@app.post("/transcription")
async def transcription(
    CallSid: str = Form(...),
    From: str = Form("Unknown"),
    TranscriptionText: str = Form(""),
    RecordingUrl: str = Form("")
):
    issue = CallData(
        name=From,
        title="Inbound Phone Call",
        description=TranscriptionText or "(no transcription)",
        priority="medium",
        raw_transcription=TranscriptionText or "(empty)"
    )
    with store_lock:
        issues_store.append(issue)

    return {"status": "saved"}

# fetch all stored issues
@app.get("/poll/")
def poll():
    with store_lock:
        return {"issues": issues_store, "log": log_store}

# poll and refresh the issue-stores
@app.get("/poll_and_clear/")
def poll_and_clear():
    with store_lock:
        issues = issues_store.copy()
        issues_store.clear()
    return {"issues": issues}
