import os, time
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Any, Optional

app = FastAPI()
START = time.time()

# In-memory stores
contexts: dict[tuple[str, str], dict] = {}
conversations: dict[str, list] = {}


@app.get("/")
async def root():
    return {"status": "Vera AI Bot is running and ready to receive judge requests!"}

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _ in contexts.items():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Antigravity",
        "team_members": ["Vera Bot Builder"],
        "model": "deterministic-templates",
        "approach": "Deterministic routing based on triggers",
        "contact_email": "bot@example.com",
        "version": "1.0.0",
        "submitted_at": datetime.utcnow().isoformat() + "Z"
    }


class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str

@app.post("/v1/context")
async def push_context(body: CtxBody):
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": datetime.utcnow().isoformat() + "Z"}


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []

@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers:
        trg = contexts.get(("trigger", trg_id), {}).get("payload")
        if not trg: continue
        
        merchant_id = trg.get("merchant_id")
        merchant = contexts.get(("merchant", merchant_id), {}).get("payload")
        if not merchant: continue
        
        category = contexts.get(("category", merchant.get("category_slug")), {}).get("payload")
        
        kind = trg.get("kind", "")
        m_name = merchant.get("identity", {}).get("name", "Merchant")
        owner_name = merchant.get("identity", {}).get("owner_first_name", m_name)
        
        # Determine language greeting
        langs = merchant.get("identity", {}).get("languages", ["en"])
        greeting = "Hi"
        if "hi" in langs:
            greeting = "Namaste"
            
        body_text = f"{greeting} {owner_name}, "
        rationale = ""
        
        # Build deterministic message based on trigger kind
        if kind == "research_digest":
            top_item_id = trg.get("payload", {}).get("top_item_id")
            digest_item = None
            if category and "digest" in category:
                for item in category["digest"]:
                    if item.get("id") == top_item_id:
                        digest_item = item
                        break
            
            if digest_item:
                title = digest_item.get("title", "")
                source = digest_item.get("source", "industry journal")
                trial_n = digest_item.get("trial_n", "")
                
                body_text += f"{source} just released new findings: {title} based on a trial of {trial_n}. "
                body_text += "Want me to draft a quick WhatsApp update you can share with your customers? Reply YES or STOP."
                rationale = "Anchoring on specific research data (source, trial N) with a low-friction YES/STOP CTA."
            else:
                body_text += f"We have some new industry research for {merchant.get('category_slug', 'your category')}. Want to see it? Reply YES or STOP."
                rationale = "Generic fallback due to missing digest item."
                
        elif kind == "recall_due":
            customer_id = trg.get("customer_id")
            customer = contexts.get(("customer", customer_id), {}).get("payload")
            c_name = customer.get("identity", {}).get("name", "Customer") if customer else "a customer"
            last_visit = customer.get("relationship", {}).get("last_visit", "a while ago") if customer else "a while ago"
            
            # Active offer
            offers = merchant.get("offers", [])
            active_offer = next((o for o in offers if o.get("status") == "active"), None)
            offer_text = f"We have an offer: {active_offer['title']}." if active_offer else "Time for a check-up."
            
            body_text = f"Just a heads up, {c_name} hasn't visited since {last_visit}. Their recall is due. Want me to send them a reminder with '{offer_text}' on your behalf? Reply YES or STOP."
            rationale = "Using specific customer name and last visit date with a clear externalization of effort."
            
        elif kind == "perf_dip":
            views = merchant.get("performance", {}).get("views", 0)
            calls = merchant.get("performance", {}).get("calls", 0)
            body_text += f"I noticed your views ({views}) and calls ({calls}) dipped recently. Want me to run a quick local campaign to boost visibility? Reply YES or STOP."
            rationale = "Loss aversion using specific performance numbers and a clear CTA."
            
        elif kind == "milestone_reached":
            body_text += f"Congratulations on reaching a new milestone! Your profile is looking great. Want me to post an update to your customers? Reply YES or STOP."
            rationale = "Positive reinforcement with effort externalization CTA."
            
        else:
            body_text += f"I noticed an update on your profile. Reply YES if you want to review it, or STOP."
            rationale = "Generic fallback for unknown trigger kind."
            
        actions.append({
            "conversation_id": f"conv_{merchant_id}_{trg_id}",
            "merchant_id": merchant_id, 
            "customer_id": trg.get("customer_id"),
            "send_as": "vera", 
            "trigger_id": trg_id,
            "template_name": "vera_generic_v1",
            "template_params": [owner_name],
            "body": body_text, 
            "cta": "binary",
            "suppression_key": trg.get("suppression_key", ""),
            "rationale": rationale
        })
    return {"actions": actions}


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int

@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conversations.setdefault(body.conversation_id, []).append({"from": body.from_role, "msg": body.message})
    
    msg_lower = body.message.lower()
    
    # Auto-reply detection
    history = conversations[body.conversation_id]
    merchant_msgs = [m["msg"] for m in history if m["from"] == "merchant"]
    if len(merchant_msgs) >= 3 and len(set(merchant_msgs[-3:])) == 1:
        return {
            "action": "end",
            "rationale": "Detected identical auto-reply 3 times in a row, gracefully exiting."
        }
    
    # Hostile detection
    if any(w in msg_lower for w in ["stop", "spam", "unsubscribe", "annoying"]):
        return {
            "action": "end",
            "rationale": "Merchant requested to stop or is hostile, exiting."
        }
        
    # Intent transition detection
    qualifying = ["would you", "do you", "what if", "how about"]
    actioning = ["yes", "ok", "sure", "do it", "go ahead", "done", "confirm"]
    
    if any(w in msg_lower for w in actioning):
        return {
            "action": "send",
            "body": "Done! I have initiated the action. I'll let you know once it's fully complete.",
            "cta": "none",
            "rationale": "Merchant committed to the action, transitioning to action mode immediately."
        }
        
    if "wait" in msg_lower or "later" in msg_lower or "busy" in msg_lower:
        return {
            "action": "wait",
            "wait_seconds": 3600,
            "rationale": "Merchant is busy, backing off for 1 hour."
        }
        
    return {
        "action": "send",
        "body": "Got it. Let me know if you need anything else! Just reply YES when ready.",
        "cta": "open_ended",
        "rationale": "Acknowledged."
    }
