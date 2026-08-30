from flask import (
    Blueprint, render_template, jsonify, request, send_from_directory, session, redirect, url_for,
    Response, stream_with_context
)
import json
import datetime
import uuid
import os
from PIL import Image
from rag.chain import app_graph, stream_answer

from aws.s3 import get_s3_presigned_url
from aws.dynamodb import (
    upsert_conversation, list_conversations,
    get_conversation, delete_conversation_from_db,
    save_report
)
from rag.gaps import looks_unanswered, record_gap
from rag.feedback import record_vote, get_vote, get_votes
from rag.roles import resolve_role


from rag.escalation import (
    create_escalation,
    mark_escalation_read,
    my_escalations,
    ROUTES,
    valid_contact,
)

from .utils import get_session_id, is_admin
from src.helper import encode_image



bp = Blueprint('chat', __name__, url_prefix='/chat')

def _is_guest():
    """Returns True if the current session belongs to a guest user."""
    return session.get("is_guest", False) or session.get("user") == "guest"


@bp.route("/")
def chat_page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    start_new = session.pop('start_new_chat', False)
    user_obj = {
        "email":    session.get("user"),
        "username": session.get("username", session.get("user", "").split("@")[0]),
        "is_guest": _is_guest(),
    }
    return render_template("chat.html", user=user_obj, start_new=str(start_new).lower())


@bp.route("/get", methods=["POST"])
def chat():
    if not session.get("user"):
        return jsonify({"error": "Please log in to use the chatbot."}), 401
    try:
        msg = request.form.get("msg", "")
        guest = _is_guest()

        # Image handling
        image_data = None
        image_mime = "image/jpeg"
        if 'image' in request.files and request.files['image'].filename != '':
            try:
                uploaded = request.files['image']
                img = Image.open(uploaded)
                fmt = (img.format or "JPEG").upper()
                image_mime = "image/png" if fmt == "PNG" else "image/jpeg"
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                    image_mime = "image/jpeg"
                image_data = encode_image(img)
            except Exception as img_err:
                print(f"Error processing upload: {img_err}")

        session_id = get_session_id()
        config = {"configurable": {"thread_id": session_id}}

        conv_id = None if guest else session.get("current_conv_id")
        current_db_history = []

        if not guest and conv_id:
            conversation = get_conversation(session.get("uid"), conv_id)
            if conversation and "messages" in conversation:
                current_db_history = conversation["messages"]
                app_graph.update_state(config, values={"chat_history": current_db_history})
        elif guest:
            # 🚀 NEW: Retrieve temporary session memory for the guest
            state = app_graph.get_state(config)
            if state and hasattr(state, 'values'):
                current_db_history = state.values.get("chat_history", [])
            
            if current_db_history:
                app_graph.update_state(config, values={"chat_history": current_db_history})

        # 🚀 FIX: Handle Conversation ID & Session updates BEFORE the streaming generator starts
        is_new_conversation = False
        conv_title = None
        created_at = None

        if not guest:
            if not conv_id:
                conv_id = str(uuid.uuid4())
                created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                session["current_conv_id"] = conv_id
                session["created_at"]      = created_at
                is_new_conversation        = True
                conv_title = msg[:40] if msg else "Image Query"
            else:
                created_at = session.get("created_at")

        input_payload = {
            "input":      msg,
            "image_data": image_data if image_data else None,
            "image_mime": image_mime  if image_data else None,
            "uid":        None if guest else session.get("uid"),
            "user_email": None if guest else session.get("user"),
            "data_consent": session.get("data_consent", False),
            # Who is asking. The graph has had an `is_faculty` field since the
            # announcements work, but nothing ever populated it — so every
            # request, the registrar's included, was answered as a student and
            # faculty-scoped notices could not actually reach anyone. The role
            # is normalised in rag/roles.py rather than here so that "who counts
            # as faculty" has one definition instead of one per call site.
            "role": resolve_role(session.get("role"), is_guest=guest),
        }


        result = app_graph.invoke(input_payload, config=config)
        messages_to_llm = result.get("messages_to_llm", [])

        # The files/pages the answer is built from. Computed during retrieval
        # (rag/citations.py) because that is the only point where provenance
        # still exists — the prompt is a flat string by the time it ships.
        citations = result.get("citations", []) or []

        if not messages_to_llm:
            return jsonify({"error": "Context compilation failed. Please try again."}), 500


        # 🚀 Server-Sent Events Token Streaming Loop
        @stream_with_context
        def generate():
            full_answer = ""
            try:
                # stream_answer() (rag/chain.py) walks the providers itself.
                # `chatModel.stream()` could not: with_fallbacks wraps the CALL,
                # but a stream fails INSIDE the caller's loop — long after the
                # wrapper returned — so a provider refusal such as AgentRouter's
                # 400 `content-blocked` on "latin honor" never reached the
                # fallback and surfaced here as "Streaming interrupted."
                for chunk_text in stream_answer(messages_to_llm):
                    full_answer += chunk_text
                    yield f"data: {json.dumps({'type': 'chunk', 'text': chunk_text})}\n\n"
            except Exception as stream_err:
                print(f"Streaming pipeline breakdown: {stream_err}")
                yield f"data: {json.dumps({'type': 'error', 'text': 'Streaming interrupted.'})}\n\n"
                return

            # Every provider answered without raising, yet nothing arrived. This
            # is a content filter succeeding quietly (HTTP 200, empty body), and
            # it used to save a blank assistant turn to the conversation — a
            # permanent hole in the history that also became a phantom 0-length
            # answer in the feedback and content-gap tables.
            if not full_answer.strip():
                print("Streaming produced no text (provider returned an empty response).")
                yield f"data: {json.dumps({'type': 'error', 'text': 'The assistant could not generate a response to that. Please rephrase your question and try again.'})}\n\n"
                return


            # 🚀 1. Combine the current interaction into the history array
            #
            # The assistant turn carries `msg_id` and `citations` alongside the
            # text. Both were previously born and buried in the browser: the id
            # was generated in JS and the citations arrived on the SSE 'done'
            # event, so reopening a saved conversation produced fresh random ids
            # and no sources — which silently detached every stored vote from the
            # answer it judged and made the "Based on" footer vanish on reload.
            #
            # Persisting them here makes the answer, its provenance and its vote
            # key one durable record. `rag/chain.py` reads only `role`/`content`
            # from history, so the extra keys are inert to the model.
            answer_msg_id = f"msg-{uuid.uuid4().hex[:16]}"
            new_messages = [
                {"role": "user",      "content": msg + (" [Image Uploaded]" if image_data else "")},
                {"role": "assistant", "content": full_answer, "msg_id": answer_msg_id},
            ]
            full_history_to_save = current_db_history + new_messages


            # 🚀 1b. CONTENT GAP LOGGING
            #
            # The prompt forces the assistant to admit ignorance when the
            # retrieved pages don't answer the question. Without this, that
            # admission is a dead end: the student leaves empty-handed and
            # nobody learns the topic is missing from the corpus. Logging it
            # turns every dead end into a ranked "documents to add" list for
            # the admin — including for guests, since an anonymous unanswered
            # question is just as much a content gap as a signed-in one.
            #
            # Detection is text-based (see rag.gaps) so it costs no extra LLM
            # call, and record_gap() swallows its own errors so a logging
            # failure can never break the reply the student already received.
            unanswered = False
            if msg and looks_unanswered(full_answer):
                unanswered = True
                record_gap(
                    msg,
                    answer=full_answer,
                    asked_by="guest" if guest else (session.get("user") or ""),
                    conv_id=conv_id or "",
                )
                print(f"  [gaps] logged unanswered question: {msg[:80]!r}")

            # 🚀 1c. CITATIONS
            #
            # Sent only when the assistant actually answered. Listing the pages
            # under "I don't have that information" would imply those pages were
            # relevant, which is the opposite of what just happened — and it is
            # the same `looks_unanswered()` verdict driving both, so the two can
            # never disagree.
            shown_citations = [] if unanswered else citations

            # Stored on the turn as well, so the footer can be rebuilt when the
            # conversation is reopened instead of only existing for as long as the
            # tab stays open.
            full_history_to_save[-1]["citations"] = shown_citations

            # 🚀 2. ALWAYS update the LangGraph state so both logged-in users and guests have temporary memory for the next turn
            app_graph.update_state(config, values={"chat_history": full_history_to_save})

            # 🚀 3. If guest, yield done and exit WITHOUT saving to the DynamoDB database
            if guest:
                yield f"data: {json.dumps({'type': 'done', 'conv_id': None, 'new_conversation_created': False, 'new_conv_title': None, 'unanswered': unanswered, 'citations': shown_citations, 'msg_id': answer_msg_id})}\n\n"
                return


            # 🚀 4. DB Save strictly for Logged-In Users
            if session.get("uid"):
                upsert_conversation(session.get("uid"), conv_id, full_history_to_save, created_at)

            # `msg_id` travels with 'done' so the element the browser just built
            # adopts the id the answer was SAVED under. Without that handshake the
            # vote on a live answer is filed against a throwaway id and is lost
            # the moment the page reloads.
            yield f"data: {json.dumps({'type': 'done', 'conv_id': conv_id, 'new_conversation_created': is_new_conversation, 'new_conv_title': conv_title, 'unanswered': unanswered, 'citations': shown_citations, 'msg_id': answer_msg_id})}\n\n"




        return Response(generate(), mimetype='text/event-stream')

    except Exception as e:
        print(f"Error in /get endpoint: {e}")
        return jsonify({"answer": f"Sorry, an error occurred: {str(e)}"}), 500


@bp.route("/report", methods=["POST"])
def submit_report():
    """
    Body: {msg_id, msg_snippet, reason, other_text, conv_id, question, sources[]}

    This is where a report is *created*, and the only place. It is reached by
    answering "What went wrong?" after a 👎.

    The downvote deliberately no longer files one by itself. A bare 👎 says an
    answer was unhelpful, which is a number; a report is a work order that an
    admin will open, read and act on. Filing one for every thumb filled the queue
    with rows whose only content was "someone disliked this", and the reasoned
    complaints — the ones that name a wrong figure or an outdated date — were
    buried among them. The vote is still recorded either way (see
    /chat/feedback), so nothing is lost by waiting for the reason: the
    satisfaction metric counts the thumb, the queue holds only what someone
    actually explained.

    `save_report()` is keyed on `msg_id`, so picking a second reason for the same
    answer corrects that row instead of adding another.

    Guests are turned away because a report is a work order addressed to a
    person: the admin has to be able to come back to whoever raised it.
    """

    if not session.get("user"):
        return jsonify({"error": "Not authenticated"}), 401
    if _is_guest():
        return jsonify({"error": "Guests cannot submit reports. Please log in."}), 403
    try:
        data        = request.get_json()
        msg_snippet = data.get("msg_snippet", "")
        reason      = data.get("reason", "")
        other_text  = data.get("other_text", "")
        conv_id     = data.get("conv_id")
        msg_id      = data.get("msg_id", "")

        save_report({
            "reporter_email": session.get("user"),
            "reporter_uid":   session.get("uid"),
            "conv_id":        conv_id,
            "msg_id":         msg_id,
            "reason":         reason,
            "other_text":     other_text,
            "msg_snippet":    msg_snippet,
            "question":       data.get("question", ""),
            "sources":        data.get("sources") or [],
            "source":         "downvote",
        })
        return jsonify({"status": "ok"})
    except Exception as e:
        print(f"Error saving report: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/feedback", methods=["POST"])
def submit_feedback():
    """
    Body: {msg_id, verdict: "up"|"down", question, answer, sources[], comment}

    One click, no form. There used to be a separate flag button next to it, which
    asked the student to fill in a form to say the same thing they had just said
    by clicking 👎 — so the votes came in and the reports did not, and nobody
    could tell whether a downvote meant "wrong" or "unhelpful".

    The vote is always recorded, and that is all this endpoint does. A 👎 is
    counted, grouped by topic, and stored with the sources that produced the
    answer — enough for the satisfaction metric and the suspect-document list. It
    does NOT file a report: a report is a work order someone has to read and act
    on, and "a student disliked this" is not yet a work order. `/chat/report`
    creates that, once the student says what was actually wrong.

    Guests are allowed to vote. Their answers are produced by the same retrieval
    pipeline, so their opinion is exactly as diagnostic as a signed-in student's,
    and requiring login here would silence the largest group of users.
    """

    if not session.get("user"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    msg_id = (data.get("msg_id") or "").strip()
    verdict = (data.get("verdict") or "").strip().lower()

    if not msg_id:
        return jsonify({"success": False, "message": "Missing message id."}), 400
    if verdict not in ("up", "down"):
        return jsonify({"success": False, "message": "Verdict must be 'up' or 'down'."}), 400

    conv_id = (data.get("conv_id") or "") or (session.get("current_conv_id") or "")

    topic = record_vote(
        msg_id,
        verdict,
        question=data.get("question") or "",
        answer=data.get("answer") or "",
        # The sources are the point: a verdict without the documents that produced
        # it tells you something is wrong but never what. See rag/feedback.py.
        sources=data.get("sources") or [],
        comment=data.get("comment") or "",
        voter="guest" if _is_guest() else (session.get("user") or ""),
        conv_id=conv_id,
    )
    if topic is None:
        return jsonify({"success": False, "message": "Could not record that vote."}), 500

    # Nothing is filed here. `can_report` only tells the UI whether it is worth
    # asking "what went wrong?" — a guest cannot file a report (no address to
    # reply to), so putting the question in front of them would collect an answer
    # with nowhere to go.
    return jsonify({
        "success": True,
        "message": "Thanks for the feedback!",
        "topic": topic,
        "can_report": verdict == "down" and not _is_guest(),
    })



@bp.route("/feedback/<msg_id>", methods=["GET"])
def read_feedback(msg_id):
    """Lets the UI restore the 👍/👎 state for a single answer."""
    if not session.get("user"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    vote = get_vote(msg_id)
    return jsonify({"success": True, "vote": vote})


@bp.route("/feedback/batch", methods=["POST"])
def read_feedback_batch():
    """
    Body: {msg_ids: [...]} → {votes: {msg_id: vote}}

    Reopening a conversation has to restore the thumb on every answer in it. Done
    one id at a time that is one request per answer — a long thread would fire
    twenty before the history finished painting, and each one re-reads the whole
    feedback store. This answers all of them in a single read.

    Only ids the student sends are looked up, and only verdicts come back, so
    this cannot be used to enumerate anyone else's feedback.
    """
    if not session.get("user"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    ids = data.get("msg_ids") or []
    if not isinstance(ids, list):
        return jsonify({"success": False, "message": "msg_ids must be a list."}), 400

    # Capped so a crafted body cannot ask for an unbounded scan.
    votes = get_votes([str(i) for i in ids[:100]])
    return jsonify({
        "success": True,
        "votes": {mid: v.get("verdict", "") for mid, v in votes.items()},
    })



@bp.route("/escalate", methods=["POST"])
def escalate():
    """
    Body: {question, route, contact, note, conv_id, answer}

    The other half of an unanswered question. `record_gap()` already makes sure
    the *documents* get fixed eventually; this makes sure the student who asked
    today still gets an answer, by queueing the question for a human with a
    contact address to reply to.

    Guests may escalate, but only with a contact detail — an escalation nobody can
    reply to is litter, not a request.
    """
    if not session.get("user"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"success": False, "message": "Nothing to ask."}), 400

    guest = _is_guest()
    student_email = "" if guest else (session.get("user") or "")
    contact = (data.get("contact") or "").strip()

    if not student_email and not valid_contact(contact):
        return jsonify({
            "success": False,
            "message": "Please leave an email or mobile number so we can reply.",
        }), 400

    route = (data.get("route") or "admin").strip().lower()
    if route not in ROUTES:
        return jsonify({
            "success": False,
            "message": f"Unknown office. Choose one of: {', '.join(ROUTES)}.",
        }), 400

    item = create_escalation(
        question,
        route=route,
        student_email=student_email,
        contact=contact,
        note=data.get("note") or "",
        conv_id=(data.get("conv_id") or "") or (session.get("current_conv_id") or ""),
        asked_answer=data.get("answer") or "",
    )
    if not item:
        return jsonify({"success": False, "message": "Could not send that question."}), 500

    # Remember a guest's contact so the reply inbox can find their escalations
    # later. A guest has no account to key on, so without this the reply would be
    # stored, sent, and invisible to them for the rest of the session.
    if guest and contact:
        session["escalate_contact"] = contact

    return jsonify({
        "success": True,
        "message": (
            f"Sent to the {item['route_label']}. They will reply to {item['reply_to']}, "
            "and the answer will also appear in your Replies inbox here."
        ),
        "escalation": {"id": item["id"], "route_label": item["route_label"]},
    })



@bp.route("/escalate/routes", methods=["GET"])
def escalation_routes():
    """The offices a question can be sent to — kept server-side so the UI list
    cannot drift out of sync with what the API will accept."""
    return jsonify({"success": True, "routes": ROUTES})


@bp.route("/escalations/mine", methods=["GET"])
def my_escalation_replies():
    """
    The student's own escalations and any replies to them.

    Without this the feature is a mailbox hand-off: the reply is stored, the admin
    can see it, and the person who asked cannot. `/chat/escalate` promises "they
    will reply", and an app that makes a promise should be the place it is kept.

    Guests are served too, keyed on the contact they typed. Their escalations are
    the ones most likely to be about enrolling — the reason they have no account
    yet — so excluding them would withhold replies from the students who need them
    most. A guest whose session has no `escalate_contact` simply sees an empty
    list, because there is nothing to look them up by.
    """
    if not session.get("user"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    guest = _is_guest()
    items = my_escalations(
        student_email="" if guest else (session.get("user") or ""),
        contact=session.get("escalate_contact") or "",
    )
    return jsonify({
        "success": True,
        "items": items,
        # Counted server-side: the badge and the list must agree, and the client
        # should not have to re-derive a rule the server already applies.
        "unread": sum(1 for i in items if i.get("unread")),
    })


@bp.route("/escalations/<esc_id>/read", methods=["POST"])
def read_escalation_reply(esc_id):
    """Acknowledge one reply so it stops being badged as new."""
    if not session.get("user"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    guest = _is_guest()
    ok = mark_escalation_read(
        esc_id,
        student_email="" if guest else (session.get("user") or ""),
        contact=session.get("escalate_contact") or "",
    )
    if not ok:
        # Deliberately not distinguishing "no such reply" from "not yours": the
        # difference is only useful to someone probing for other people's ids.
        return jsonify({"success": False, "message": "No such reply."}), 404
    return jsonify({"success": True})



@bp.route('/document/<filename>')
def serve_document(filename):

    current_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(current_dir, '..', 'data')
    local_path = os.path.join(data_dir, filename)
    
    # 1. Check if the file exists locally (e.g., initial seed documents)
    if os.path.exists(local_path):
        return send_from_directory(data_dir, filename)
        
    # 2. If not found locally, fetch the presigned URL from S3
    try:
        s3_url = get_s3_presigned_url(filename, for_download=False)
        if s3_url:
            return redirect(s3_url)
    except Exception as e:
        print(f"Error fetching {filename} from S3: {e}")
        
    # 3. If neither worked, return a 404
    return "Document not found", 404


@bp.route("/clear", methods=["POST"])
def clear_memory():
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"})
    session.pop("session_id",       None)
    session.pop("current_conv_id",  None)
    session.pop("created_at",       None)
    return jsonify({"status": "success", "message": "New session started"})


@bp.route("/conversations", methods=["GET"])
def conversations():
    if not session.get("user") or _is_guest():
        return jsonify([])
    return jsonify(list_conversations(session.get("uid")))


@bp.route("/conversation/<conv_id>", methods=["GET"])
def conversation(conv_id):
    if not session.get("user") or _is_guest():
        return jsonify({"error": "Not authenticated"}), 401
    conv = get_conversation(session.get("uid"), conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    return jsonify(conv)


@bp.route("/conversation/<conv_id>/restore", methods=["POST"])
def restore_conversation(conv_id):
    if not session.get("user") or _is_guest():
        return jsonify({"error": "Not authenticated"}), 401
    conv = get_conversation(session.get("uid"), conv_id)
    if not conv or "messages" not in conv:
        return jsonify({"error": "Conversation not found"}), 404

    session_id = get_session_id()
    app_graph.update_state(
        config={"configurable": {"thread_id": session_id}},
        values={"chat_history": conv["messages"]},
    )
    session["current_conv_id"] = conv_id
    session["created_at"]      = conv.get(
        "created_at",
        datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    return jsonify({"status": "success", "message": "Conversation restored"})


@bp.route("/conversation/<conv_id>/delete", methods=["DELETE"])
def delete_conversation(conv_id):
    if not session.get("user") or _is_guest():
        return jsonify({"error": "Not authenticated"}), 401
    delete_conversation_from_db(session.get("uid"), conv_id)
    return jsonify({"status": "success", "message": "Conversation deleted"})


@bp.route("/index")
def index():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    return redirect(url_for("admin.dashboard") if is_admin() else url_for("chat.chat_page"))