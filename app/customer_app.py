"""Customer application - minimal one-window UI.

Screen flow (exact labels required by the shop):
    1. one large button:            "tech check"
    2. code entry screen:           "confirm code"
    3. after the correct code:      "GETTING READY"   (nothing else on screen)
    4. when a technician requests:  consent screen (view / control approval)
    5. session screen:              visible sharing status + "end" button

The support code is 1234 (configurable with --code).
The window is plain Tk so it runs on any Ubuntu desktop or an Xvfb/Openbox
session without extra UI toolkits.

Run:  .venv/bin/python -m app.customer_app --display 127.0.0.1:106
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tkinter as tk
from tkinter import font as tkfont

from . import config as configmod
from .customer import CustomerConfig, CustomerCallbacks, CustomerSession, CustomerState
from .displayutil import DisplayError, verify_display
from .version import APP_VERSION

log = logging.getLogger("support.customer-app")

BG = "#f4f6f9"
FG = "#1b2733"
ACCENT = "#2f7be5"
ACCENT_HOVER = "#245fb8"
DANGER = "#d64545"
MUTED = "#5c6b7a"

SUPPORT_CODE = "1234"

SESSION_TEXT = "connected. the technician is working. give us a moment."


class CustomerApp(CustomerCallbacks):
    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.session: CustomerSession | None = None
        self.last_error_report = ""
        self.expected_code = args.code
        self.code_var = tk.StringVar()

        root.title("Support")
        root.configure(bg=BG)
        root.geometry("560x430")
        root.minsize(480, 380)

        self.big = tkfont.Font(family="TkDefaultFont", size=22, weight="bold")
        self.huge = tkfont.Font(family="TkDefaultFont", size=26, weight="bold")
        self.body = tkfont.Font(family="TkDefaultFont", size=13)
        self.small = tkfont.Font(family="TkDefaultFont", size=11)

        self._frames: dict[str, tk.Frame] = {}
        self._build_home()
        self._build_code()
        self._build_ready()
        self._build_consent()
        self._build_session()
        self._build_error()
        self.show("home")
        try:
            root.eval("tk::PlaceWindow . center")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ UI

    def _frame(self, name: str) -> tk.Frame:
        frame = tk.Frame(self.root, bg=BG)
        self._frames[name] = frame
        return frame

    def _build_home(self) -> None:
        f = self._frame("home")
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)
        btn = tk.Button(
            f, text="tech check", font=self.huge, fg="#ffffff", bg=ACCENT,
            activebackground=ACCENT_HOVER, activeforeground="#ffffff",
            relief="flat", bd=0, padx=46, pady=26, cursor="hand2",
            command=self.on_tech_check,
        )
        btn.grid(row=0, column=0, sticky="")
        self.home_btn = btn
        tk.Label(
            f, text=f"v{APP_VERSION}", font=self.small, bg=BG, fg=MUTED
        ).grid(row=1, column=0, sticky="s", pady=(0, 8))

    def _build_code(self) -> None:
        f = self._frame("code")
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)
        inner = tk.Frame(f, bg=BG)
        inner.grid(row=0, column=0)
        tk.Label(inner, text="confirm code", font=self.big, bg=BG, fg=FG).pack(pady=(0, 26))
        entry = tk.Entry(
            inner, textvariable=self.code_var, font=tkfont.Font(size=20),
            justify="center", width=12, relief="solid", bd=1,
        )
        entry.pack(pady=4)
        self.code_entry = entry
        tk.Button(
            inner, text="ok", font=self.body, fg="#ffffff", bg=ACCENT,
            activebackground=ACCENT_HOVER, relief="flat", bd=0,
            padx=34, pady=9, cursor="hand2", command=self.on_submit_code,
        ).pack(pady=18)
        self.code_error = tk.Label(inner, text="", font=self.small, bg=BG, fg=DANGER)
        self.code_error.pack()
        entry.bind("<Return>", lambda _e: self.on_submit_code())

    def _build_ready(self) -> None:
        f = self._frame("ready")
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)
        tk.Label(f, text="GETTING READY", font=self.big, bg=BG, fg=FG).grid(
            row=0, column=0, sticky=""
        )

    def _build_consent(self) -> None:
        f = self._frame("consent")
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)
        inner = tk.Frame(f, bg=BG)
        inner.grid(row=0, column=0)
        tk.Label(inner, text="a technician is coming",
                 font=self.big, bg=BG, fg=FG).pack(pady=(0, 10))
        tk.Label(inner, text="are you sure about this?",
                 font=self.body, bg=BG, fg=MUTED).pack()
        row = tk.Frame(inner, bg=BG)
        row.pack(pady=22)
        tk.Button(
            row, text="yes", font=self.body, fg="#ffffff", bg=ACCENT,
            activebackground=ACCENT_HOVER, relief="flat", bd=0, padx=30, pady=9,
            cursor="hand2", command=self.on_allow,
        ).pack(side="left", padx=8)
        tk.Button(
            row, text="no", font=self.body, fg="#ffffff", bg=DANGER,
            activebackground="#b23636", relief="flat", bd=0, padx=30, pady=9,
            cursor="hand2", command=self.on_decline,
        ).pack(side="left", padx=8)

    def _build_session(self) -> None:
        f = self._frame("session")
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)
        inner = tk.Frame(f, bg=BG)
        inner.grid(row=0, column=0)
        self.session_status = tk.Label(
            inner, text="connecting…", font=self.big, bg=BG, fg=FG, wraplength=460
        )
        self.session_status.pack(pady=(0, 8))
        self.session_detail = tk.Label(
            inner, text="", font=self.small, bg=BG, fg=MUTED, wraplength=460
        )
        self.session_detail.pack(pady=(0, 26))
        tk.Button(
            inner, text="end", font=self.big, fg="#ffffff", bg=DANGER,
            activebackground="#b23636", relief="flat", bd=0, padx=52, pady=16,
            cursor="hand2", command=self.on_end,
        ).pack()

    def _build_error(self) -> None:
        f = self._frame("error")
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)
        top = tk.Frame(f, bg=BG)
        top.grid(row=0, column=0, pady=(18, 0))
        self.error_label = tk.Label(
            top, text="", font=self.body, bg=BG, fg=DANGER, wraplength=520,
            justify="left",
        )
        self.error_label.pack(pady=(0, 8))
        self.error_report_label = tk.Label(
            top, text="", font=self.small, bg=BG, fg=MUTED, wraplength=520,
            justify="left",
        )
        self.error_report_label.pack(pady=(0, 12))
        tk.Button(
            top, text="try again", font=self.body, fg="#ffffff", bg=ACCENT,
            activebackground=ACCENT_HOVER, relief="flat", bd=0, padx=30, pady=9,
            cursor="hand2", command=self.on_retry,
        ).pack()
        # The complete, exact diagnosis: every step, the traceback, and each
        # capability checked one by one.  Never hidden - the customer can read
        # it out or copy it to the technician as-is.
        body = tk.Frame(f, bg=BG)
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=(12, 14))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.error_details = tk.Text(
            body, font=("TkFixedFont", 9), bg="#14161a", fg="#d7dce4",
            relief="flat", bd=0, wrap="none", state="disabled",
        )
        self.error_details.grid(row=0, column=0, sticky="nsew")
        ys = tk.Scrollbar(body, command=self.error_details.yview)
        ys.grid(row=0, column=1, sticky="ns")
        self.error_details.configure(yscrollcommand=ys.set)

    def show(self, name: str) -> None:
        for frame in self._frames.values():
            frame.grid_forget()
        self._frames[name].grid(row=0, column=0, sticky="nsew")
        self._frames[name].grid_rowconfigure(0, weight=1)
        self._frames[name].grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        if name == "code":
            self.code_entry.focus_set()

    # ------------------------------------------------------------- actions

    def on_tech_check(self) -> None:
        self.code_error.config(text="")
        self.code_var.set("")
        self.show("code")

    def on_submit_code(self) -> None:
        code = self.code_var.get().strip()
        if code != self.expected_code:
            self.code_error.config(text="wrong code, try again")
            return
        self.show("ready")
        self.start_session(code)

    def start_session(self, code: str) -> None:
        cfg = CustomerConfig(
            signaling_url=self.args.signaling_url,
            code=code,
            display=self.args.display,
            fps=self.args.fps,
            max_width=self.args.max_width,
            capture_backend=self.args.capture_backend,
        )
        self.session = self._session_factory(cfg, self)
        self.session.start_background()

    def on_allow(self) -> None:
        if self.session is None:
            return
        # "yes" = the technician may see the screen and work on the machine.
        self.session.grant_consent(True, True)
        self._show_session_screen()

    def on_decline(self) -> None:
        if self.session is not None:
            self.session.deny()
        self._restart_flow("access declined")

    def on_end(self) -> None:
        if self.session is not None:
            self.session.stop("customer ended the session")
        self._restart_flow("session ended")

    def on_retry(self) -> None:
        self._restart_flow("")

    def _restart_flow(self, message: str) -> None:
        self.session = None
        self.show("home")
        if message:
            log.info(message)

    def _show_session_screen(self) -> None:
        self.session_status.config(text=SESSION_TEXT)
        self.session_detail.config(text="")
        self.show("session")

    # ------------------------------------------------- CustomerCallbacks

    def on_state(self, state: CustomerState, message: str = "") -> None:
        self.root.after(0, lambda: self._ui_state(state, message))

    def on_technician_present(self) -> None:
        self.root.after(0, lambda: self.show("consent"))

    def on_session_ended(self, reason: str) -> None:
        self.root.after(0, lambda: self._ui_ended(reason))

    def on_error(self, message: str) -> None:
        self.root.after(0, lambda: self._ui_error(message))

    def _ui_state(self, state: CustomerState, message: str) -> None:
        if self.session is None:
            return
        if state == CustomerState.WAITING:
            pass  # GETTING READY stays on screen while waiting
        elif state == CustomerState.CONSENT:
            self.show("consent")
        elif state in (CustomerState.ACTIVE, CustomerState.CONNECTING_SESSION):
            self._show_session_screen()

    def _ui_ended(self, reason: str) -> None:
        self.last_error_report = (
            getattr(self.session, "last_error_report", "") or self.last_error_report
        )
        self.session = None
        self.show("home")
        log.info("session ended: %s", reason)

    def _ui_error(self, message: str) -> None:
        report = self.last_error_report
        self.error_label.config(text=message)
        self.error_details.configure(state="normal")
        self.error_details.delete("1.0", "end")
        if report:
            self.error_report_label.config(
                text="the full diagnosis is below - copy it to the technician "
                "(also saved to ~/.local/share/tech-support/last-error.txt)"
            )
            self.error_details.insert("1.0", report)
        else:
            self.error_report_label.config(text="")
        self.error_details.configure(state="disabled")
        self.root.geometry("720x620")
        self.show("error")

    # ------------------------------------------------------------ factory

    def _session_factory(self, cfg: CustomerConfig, cb: CustomerCallbacks) -> CustomerSession:
        return CustomerSession(cfg, cb)


def build_arg_parser() -> argparse.ArgumentParser:
    cfg = configmod.load_config()
    parser = argparse.ArgumentParser(description="Customer support application")
    parser.add_argument("--display", default=os.environ.get("DISPLAY"),
                        help="X11 display to show the window on and to share "
                             "(e.g. :0, :106, 127.0.0.1:106)")
    parser.add_argument("--signaling-url", default=cfg.signaling_url,
                        help="signaling server websocket URL "
                             "(wss://host/ws on a real deployment)")
    parser.add_argument("--code", default=cfg.support_code,
                        help="support code the customer must enter (default: 1234)")
    parser.add_argument("--fps", type=float, default=8.0)
    parser.add_argument("--max-width", type=int, default=0,
                        help="downscale the stream to this width (0 = native 1:1)")
    parser.add_argument("--capture-backend", default="auto",
                        choices=["auto", "pipewire", "mss", "ffmpeg"],
                        help="screen-capture backend (auto escalates from the "
                             "X11 grabber to compositor capture when the grab "
                             "comes back blank, e.g. NVIDIA/Wayland desktops)")
    parser.add_argument("--self-test-ui", action="store_true",
                        help="run the UI flow self test and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)

    try:
        info = verify_display(args.display)
    except DisplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    os.environ["DISPLAY"] = info["env_name"]
    log.info("using display %s (transport: %s, %sx%s)",
             info["display"], info["transport"], info["width"], info["height"])

    root = tk.Tk()
    app = CustomerApp(root, args)

    if args.self_test_ui:
        return run_self_test(root, app)

    try:
        root.mainloop()
    finally:
        if app.session is not None:
            app.session.stop("application closed")
    return 0


def run_self_test(root: tk.Tk, app: CustomerApp) -> int:
    """Drive the real UI and assert the required screens appear."""
    failures: list[str] = []

    def check(label: str, condition: bool) -> None:
        print(f"{'ok' if condition else 'FAIL'}: {label}")
        if not condition:
            failures.append(label)

    root.update()
    check("home shows 'tech check' button", app.home_btn.cget("text") == "tech check")
    home_buttons = [
        w for w in app._frames["home"].winfo_children() if isinstance(w, tk.Button)
    ]
    check("home has no other buttons", len(home_buttons) == 1)

    app.on_tech_check()
    root.update()
    code_labels = [
        w for w in app._frames["code"].winfo_children()[0].winfo_children()
        if isinstance(w, tk.Label)
    ]
    check("code screen shows 'confirm code'",
          any(str(w.cget("text")) == "confirm code" for w in code_labels))

    app.code_var.set("0000")
    app.on_submit_code()
    root.update()
    check("wrong code stays on code screen",
          app._frames["code"].winfo_ismapped() and app.code_error.cget("text") != "")

    app.code_var.set(app.expected_code)

    class FakeSession:
        def __init__(self, cfg, cb):
            self.cfg, self.cb = cfg, cb
        def start_background(self):
            pass
        def grant_consent(self, view, control):
            pass
        def deny(self):
            pass
        def stop(self, reason=""):
            pass

    app._session_factory = lambda cfg, cb: FakeSession(cfg, cb)
    app.on_submit_code()
    root.update()
    ready_labels = [w for w in app._frames["ready"].winfo_children()]
    check("ready screen shows 'GETTING READY'",
          len(ready_labels) == 1 and str(ready_labels[0].cget("text")) == "GETTING READY")

    def collect(widget, kind):
        out = []
        for w in widget.winfo_children():
            if isinstance(w, kind):
                out.append(str(w.cget("text")))
            out.extend(collect(w, kind))
        return out

    app.on_technician_present()
    root.update()
    check("consent screen appears when technician is present",
          bool(app._frames["consent"].winfo_ismapped()))
    consent_labels = collect(app._frames["consent"], tk.Label)
    check("consent says 'a technician is coming'",
          "a technician is coming" in consent_labels)
    check("consent asks 'are you sure about this?'",
          "are you sure about this?" in consent_labels)
    check("consent offers exactly yes / no",
          collect(app._frames["consent"], tk.Button) == ["yes", "no"])
    app.on_allow()
    root.update()
    check("session screen appears after allowing",
          bool(app._frames["session"].winfo_ismapped()))
    check("session screen says the technician is working",
          str(app.session_status.cget("text")) == SESSION_TEXT)

    root.destroy()
    if failures:
        print(f"SELF-TEST FAILED: {failures}")
        return 1
    print("SELF-TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
