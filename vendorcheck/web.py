"""Review app: the compliance team's screen.

  python -m vendorcheck serve            # http://127.0.0.1:8765

- Start a vendor check (name, website, optional GSTIN). It runs in the background.
- Read the evidence file: verdict, reasons, who runs it, every fact with its source.
- Approve or decline. Irreversible actions happen only here (or on the command line), exactly once.
- Open the stored raw response behind any API fact, the cost ledger and the event log.

On start, unfinished checks are resumed, so killing the server and starting it again carries on
where it stopped. Standard library only; listens on 127.0.0.1 (this machine) by default.
"""
import hmac
import json
import secrets
import threading
import traceback
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .actions import decide_approval
from .evidence import get_path
from .html_report import page, render_job, verdict_badge
from .pipeline import Pipeline, new_job
from .report import build_result, cost_summary, save_result
from .store import Store

RUNNING: set[str] = set()                 # job ids with a background thread in this process
RUNNING_LOCK = threading.Lock()


def run_in_background(db_path: str, cfg: dict, job_id: str) -> None:
    """Run (or resume) one job on its own thread with its own database connection."""
    with RUNNING_LOCK:
        if job_id in RUNNING:
            return
        RUNNING.add(job_id)

    def work():
        store = Store(db_path)
        try:
            Pipeline(store, cfg, log=lambda message: None).run(job_id)
            save_result(store, cfg, job_id)
        except Exception:                              # the job stays resumable; the error is logged
            store.log_event(job_id, "error", detail=traceback.format_exc()[-1500:])
        finally:
            store.db.close()
            with RUNNING_LOCK:
                RUNNING.discard(job_id)

    threading.Thread(target=work, daemon=True).start()


def make_handler(db_path: str, cfg: dict):
    # A secret per server process, put into every form. A POST without it (e.g. a form on another web
    # page the reviewer happens to open) is refused, so nothing irreversible can be triggered from outside.
    csrf_token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # keep the terminal quiet; the event log has the detail
            pass

        # ------------------------------------------------------------------ helpers
        def send_html(self, html: str, status: int = 200):
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, data, status: int = 200):
            body = json.dumps(data, indent=2, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def redirect(self, location: str):
            self.send_response(303)
            self.send_header("location", location)
            self.end_headers()

        def form(self) -> dict:
            length = int(self.headers.get("content-length") or 0)
            data = parse_qs(self.rfile.read(length).decode("utf-8"))
            return {k: v[0].strip() for k, v in data.items()}

        def host_allowed(self) -> bool:
            """Only answer requests addressed to this machine (blocks DNS-rebinding reads)."""
            host = (self.headers.get("host") or "").split(":")[0].strip("[]").lower()
            return host in {"127.0.0.1", "localhost", "::1"} | set(cfg.get("web", {}).get("allowed_hosts", []))

        # ------------------------------------------------------------------ routes
        def do_GET(self):
            if not self.host_allowed():
                return self.send_html(page("Refused", "<p>Unknown host.</p>"), 403)
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            store = Store(db_path)
            try:
                if not parts:
                    return self.send_html(self.home(store))
                if parts[0] == "job" and len(parts) == 2:
                    job_id = parts[1].removesuffix(".json")
                    if not store.job(job_id):
                        return self.send_html(page("Not found", "<p>No such job.</p>"), 404)
                    result = build_result(store, cfg, job_id)
                    if parts[1].endswith(".json"):
                        return self.send_json(result)
                    return self.send_html(render_job(result, live_links=True, csrf=csrf_token))
                if parts[0] == "call" and len(parts) == 2:
                    return self.send_html(self.raw_call(store, int(parts[1]), parse_qs(url.query)))
                if parts[0] == "ledger" and len(parts) == 2:
                    return self.send_html(self.ledger(store, parts[1]))
                if parts[0] == "log" and len(parts) == 2:
                    path = store.log_dir / f"{parts[1]}.jsonl"
                    text = path.read_text() if path.exists() else "no log"
                    return self.send_html(page("Event log", f"<h1>Event log</h1><pre>{escape(text)}</pre>"))
                return self.send_html(page("Not found", "<p>Not found.</p>"), 404)
            finally:
                store.db.close()

        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if not self.host_allowed():
                return self.send_html(page("Refused", "<p>Unknown host.</p>"), 403)
            posted = self.form()
            if not hmac.compare_digest(posted.get("csrf", ""), csrf_token):
                return self.send_html(page("Refused", "<p>This form did not come from this app; nothing was "
                                                      "done. Reload the page and try again.</p>"), 403)
            self.form = lambda: posted                      # the body was read once; later calls reuse it
            store = Store(db_path)
            try:
                if parts == ["check"]:
                    f = self.form()
                    request = {k: f[k] for k in ("name", "website", "gstin", "cin", "directors", "text") if f.get(k)}
                    if not request:
                        return self.redirect("/")
                    job_id = new_job(store, request)
                    run_in_background(db_path, cfg, job_id)
                    return self.redirect(f"/job/{job_id}")
                if len(parts) == 3 and parts[0] == "job" and parts[2] == "decide":
                    f = self.form()
                    job_id = parts[1]
                    if not f.get("by"):
                        message = "Enter your name: every decision records who made it."
                    elif f.get("decision") == "decline":
                        message = decide_approval(store, job_id, f["by"], decline=True, reason=f.get("reason"),
                                                  approvers=cfg.get("approvals", {}).get("approvers"))
                    else:
                        message = decide_approval(store, job_id, f["by"], f.get("action"), reason=f.get("reason"),
                                                  approvers=cfg.get("approvals", {}).get("approvers"))
                    save_result(store, cfg, job_id)
                    return self.send_html(render_job(build_result(store, cfg, job_id), True, message, csrf_token))
                return self.send_html(page("Not found", "<p>Not found.</p>"), 404)
            finally:
                store.db.close()

        # ------------------------------------------------------------------ pages
        def home(self, store: Store) -> str:
            rows = []
            for job in reversed(store.jobs()):
                request = json.loads(job["request_json"])
                vendor = request.get("name") or request.get("website") or request.get("text", "")[:40]
                credits = store.credits_spent(job["id"])
                approval = store.db.execute("SELECT status FROM approvals WHERE job_id=?", (job["id"],)).fetchone()
                state = "running" if job["stage"] != "done" else (approval["status"] if approval else "-")
                rows.append(f"<tr><td><a href='/job/{escape(job['id'])}'>{escape(vendor)}</a></td>"
                            f"<td>{escape(request.get('website') or '')}</td>"
                            f"<td>{verdict_badge(job['verdict']) if job['verdict'] else 'running…'}</td>"
                            f"<td>{escape(state)}</td><td>{credits:g}</td>"
                            f"<td><code>{escape(job['id'])}</code></td></tr>")
            table = ("<table><tr><th>Vendor</th><th>Website</th><th>Verdict</th><th>Approval</th>"
                     "<th>Credits</th><th>Job</th></tr>" + "".join(rows) + "</table>") if rows else \
                "<p class='muted'>No checks yet.</p>"
            body = ("<div class='card'><h1>New vendor check</h1>"
                    "<form class='inline' method='post' action='/check'>"
                    f"<input type='hidden' name='csrf' value='{csrf_token}'>"
                    "<input name='name' placeholder='Vendor name' size='28'>"
                    "<input name='website' placeholder='Website, e.g. acmefreight.in' size='26'>"
                    "<input name='gstin' placeholder='GSTIN (optional)' size='18'>"
                    "<input name='cin' placeholder='CIN (optional)' size='22'>"
                    "<input name='directors' placeholder='Directors, separated by ; (optional)' size='30'>"
                    "<button>Check vendor</button></form>"
                    "<p class='muted'>The check gathers facts from Crustdata, the domain registry and the GST "
                    "registry, decides with fixed rules, and waits for a person before anything irreversible."
                    "</p></div>"
                    f"<div class='card'><h2>Checks</h2>{table}</div>"
                    f"<div class='card'><h2>Measured cost</h2><pre>{escape(cost_summary(store, cfg))}</pre></div>")
            running = any(job["stage"] != "done" for job in store.jobs())
            return page("Vendor Check", body, refresh_seconds=3 if running else None)

        def raw_call(self, store: Store, call_id: int, query: dict) -> str:
            call = store.call(call_id)
            if not call:
                return page("Not found", "<p>No such call.</p>")
            path = (query.get("path") or [""])[0]
            pointed = ""
            if path and path != "passage" and call["response_text"]:
                try:
                    value = get_path(json.loads(call["response_text"]), path)
                    pointed = (f"<div class='card'><h2>Field <code>{escape(path)}</code></h2>"
                               f"<pre>{escape(json.dumps(value, indent=2, ensure_ascii=False)[:4000])}</pre></div>")
                except Exception:
                    pointed = f"<p class='muted'>Field {escape(path)} not found in this response.</p>"
            raw = call["response_text"] or "(raw response deleted by the retention policy; the fact keeps its quote)"
            try:
                raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
            return page(f"Stored response #{call_id}",
                        f"<div class='card'><h1>Stored response #{call_id}</h1>"
                        f"<p>{escape(call['source'])} <code>{escape(call['endpoint'])}</code> · {escape(call['status'])}"
                        f" · retrieved {escape(call['retrieved_at'])} · {call['credits']} credits "
                        f"({escape(call['credits_source'] or '-')})</p>"
                        f"<p class='muted'>Request: <code>{escape(call['request_json'][:600])}</code></p></div>"
                        f"{pointed}<div class='card'><h2>Full response</h2><pre>{escape(raw[:200000])}</pre></div>")

        def ledger(self, store: Store, job_id: str) -> str:
            rows = "".join(
                f"<tr><td><a href='/call/{c['id']}'>#{c['id']}</a></td><td>{escape(c['source'])}</td>"
                f"<td><code>{escape(c['endpoint'])}</code></td><td>{escape(c['status'])}</td>"
                f"<td>{c['credits']}</td><td>{escape(c['credits_source'] or '-')}</td><td>{c['attempts']}</td>"
                f"<td>{c['latency_ms'] or 0} ms</td><td>{escape(c['retrieved_at'])}</td></tr>"
                for c in store.ledger(job_id))
            return page("Cost ledger",
                        f"<div class='card'><h1>Cost ledger — <code>{escape(job_id)}</code></h1>"
                        "<table><tr><th>Call</th><th>Source</th><th>Endpoint</th><th>Status</th><th>Credits</th>"
                        f"<th>Credits are</th><th>Tries</th><th>Latency</th><th>Retrieved</th></tr>{rows}</table>"
                        f"<p><a href='/log/{escape(job_id)}'>Event log</a> · <a href='/job/{escape(job_id)}'>"
                        "Back to the evidence file</a></p></div>")

    return Handler


def serve(db_path: str, cfg: dict, host: str = "127.0.0.1", port: int = 8765) -> None:
    store = Store(db_path)
    unfinished = [job["id"] for job in store.jobs() if job["stage"] != "done"]
    store.db.close()
    for job_id in unfinished:                     # restart safety: carry on where a killed run stopped
        run_in_background(db_path, cfg, job_id)
    server = ThreadingHTTPServer((host, port), make_handler(db_path, cfg))
    print(f"Vendor Check review app: http://{host}:{port}"
          + (f"  (resuming {len(unfinished)} unfinished check(s))" if unfinished else ""))
    server.serve_forever()
