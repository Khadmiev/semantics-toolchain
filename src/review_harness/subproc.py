# SPDX-License-Identifier: Apache-2.0
"""An observed role launch: streamed output plus the death-recognition window.

One mechanism for the critic and for development: the harness has no role whose
silence goes unwatched — a hung session of any role is recognised within the
machine's window and fails loudly, keeping the output already received.

The sign of life is ANY bytes in either pipe, not finished lines: LLM clients
stream tokens without a newline and CLIs draw progress with carriage returns —
reading line by line would declare such a role silent. The output is returned
verbatim, byte for byte after decoding: the harness neither trims nor pads
somebody else's text.

The process tree: a role is finished only together with ALL the processes it
spawned. Watching live pipes does not see a helper started with DEVNULL pipes (a
finding of round 4), so the guarantee is structural: on Windows the role lives in
a Job Object with kill-on-close — closing the job kills every surviving
descendant, even those detached from the pipes; on POSIX the role starts in its
own process group and the group is finished off after the parent exits.
"""

from __future__ import annotations

import codecs
import os
import subprocess
import threading
import time
from pathlib import Path

from .errors import HarnessError

_CHUNK = 4096


class _ProcessTree:
    """Ownership of the role's process tree — guaranteed, not heuristic."""

    def __init__(self, proc: subprocess.Popen, *, role: str) -> None:
        self.proc = proc
        self._job = None
        self._pgid: int | None = None
        if os.name == "nt":
            self._job = _create_kill_on_close_job(proc)
            if self._job is None:
                # Fail-closed: without a job there is no guarantee over the tree — the role is
                # not started "and we shall see" (a descendant on DEVNULL would outlive it).
                proc.kill()
                raise HarnessError(
                    f"Role \"{role}\" stopped: no Job Object available",
                    cause=(
                        "creating or joining a Job Object failed — "
                        "the structural guarantee that the tree dies is not provided"
                    ),
                    next_action=(
                        "check this machine's process rights and nested jobs; "
                        "without the guarantee over the tree, starting roles is forbidden"
                    ),
                )
        else:
            # start_new_session=True guarantees setsid: the pgid EQUALS the child's pid by
            # construction — it need not be asked of the OS, and the race with a parent that
            # exits quickly cannot be lost.
            self._pgid = proc.pid

    def kill(self) -> bool:
        """Kill the tree; True means death is confirmed. Idempotent.

        The job handle is cleared after the first kill: a second call (the safety-net
        handler after the timeout path) does not poke a closed handle and does not
        declare an already confirmed death unconfirmed (round 12).
        """
        if getattr(self, "_kill_confirmed", None) is not None:
            return self._kill_confirmed
        confirmed = True
        if self._job is not None:
            confirmed = _terminate_job(self._job)
            self._job = None
        elif os.name == "nt":
            rc = subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=30,
            ).returncode
            confirmed = rc in (0, 128)
        elif self._pgid is not None:
            import signal

            try:
                os.killpg(self._pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # the group is gone already — everyone is dead
            except OSError:
                confirmed = False
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            confirmed = False
        self._kill_confirmed = confirmed
        return confirmed

    def reap_survivors(self) -> bool:
        """After the parent exits, finish off the descendants. True means death confirmed.

        It is called on the successful path too: a helper that outlived its parent
        (with pipes or without) must not keep running after the role has been declared
        over — and an unconfirmed finish-off on the successful path is as much a
        refusal as one on the timeout path (a finding of round 6).
        """
        if self._job is not None:
            ok = _terminate_job(self._job)
            self._job = None
            return ok
        if self._pgid is not None:
            import signal

            try:
                os.killpg(self._pgid, signal.SIGKILL)
            except ProcessLookupError:
                return True  # the group is gone already — everyone is dead
            except OSError:
                return False
            return True
        return os.name != "nt"  # nt without a job never gets here (fail-closed)


def _create_kill_on_close_job(proc: subprocess.Popen):
    """A Windows Job Object with KILL_ON_JOB_CLOSE; None when it could not be made."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok or not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _resume_process(pid: int) -> bool:
    """Resume the main thread of a process born CREATE_SUSPENDED."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        TH32CS_SNAPTHREAD = 0x4
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snap in (0, -1):
            return False
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            ok = kernel32.Thread32First(snap, ctypes.byref(entry))
            resumed = False
            while ok:
                if entry.th32OwnerProcessID == pid:
                    THREAD_SUSPEND_RESUME = 0x0002
                    th = kernel32.OpenThread(
                        THREAD_SUSPEND_RESUME, False, entry.th32ThreadID
                    )
                    if th:
                        kernel32.ResumeThread(th)
                        kernel32.CloseHandle(th)
                        resumed = True
                ok = kernel32.Thread32Next(snap, ctypes.byref(entry))
            return resumed
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return False


def _terminate_job(job) -> bool:
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ok = bool(kernel32.TerminateJobObject(job, 1))
        kernel32.CloseHandle(job)
        return ok
    except Exception:
        return False


def run_watched(
    argv: list[str],
    *,
    role: str,
    stream_path: Path,
    detection_window_sec: int,
    input_text: str | None = None,
    privacy_check=None,
    env_overrides: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Start the role; return (stdout, stderr) verbatim, stdout streamed to a file.

    The two streams are returned separately: codex prints its launch header to
    STDERR (ready knowledge of the audience genre, isolation.py) — attestation that
    looked only at stdout would annul every honest pass. ``input_text`` is fed to the
    role on stdin (delivery of the accompanying note's content through the positional
    "-").

    Silence longer than the window is real death (the operator's reading of
    2026-09-01): the process tree is killed, the refusal names its cause and the next
    action, and the part of the output already received stays in ``stream_path``.
    """
    # Windows: the role is born SUSPENDED and is resumed only after it is assigned
    # to the Job Object — otherwise a descendant spawned before the assignment would
    # slip out of the job (the race window from the finding of round 6).
    creationflags = 0x00000004 if os.name == "nt" else 0  # CREATE_SUSPENDED
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
            creationflags=creationflags,
            # The launch environment is part of the machine profile (the spec: "command,
            # environment, sandbox"): inheriting a random shell would silently replace the
            # role's authorisation or proxy (a finding of round 9).
            env={**os.environ, **(env_overrides or {})},
        )
    except OSError as exc:
        raise HarnessError(
            f"Role \"{role}\" could not be started: {argv[0]}",
            cause=f"the OS refused to start it: {exc}",
            next_action="check the role command in the machine profile (profile-check)",
        ) from exc
    tree = _ProcessTree(proc, role=role)
    if os.name == "nt" and not _resume_process(proc.pid):
        killed = tree.kill()
        raise HarnessError(
            f"Role \"{role}\" could not be resumed after joining the job"
            + ("" if killed else " (the death of the tree is NOT confirmed)"),
            cause="ResumeThread did not find the main thread of the suspended process",
            next_action=(
                "repeat the round; if it repeats, check the process rights"
                + ("" if killed else "; find and finish off the suspended process by hand")
            ),
        )

    out_parts: list[str] = []
    err_parts: list[str] = []
    # Byte-exact evidence: if the output held non-UTF-8, the textual form with
    # escapes is ambiguous (a real 0xFF byte and its four-character escape look the
    # same) — the exact bytes are kept in a raw file beside the stream (a finding of
    # round 10).
    # The evidence is PER CHANNEL (round 19): a shared list in scheduler order
    # matched neither the journal nor the final transcript (stderr-first) byte for
    # byte; each stream's own order is unambiguous, and the position of a broken byte
    # relative to the diagnostics is exact.
    raw_out: list[bytes] = []
    raw_err: list[bytes] = []
    had_invalid = [False]
    last_activity = time.monotonic()
    lock = threading.Lock()
    # A write error on the sink has no right to die silently in a background thread:
    # a role with no working carrier for its output is a refusal, not a quiet success.
    sink_error: list[OSError] = []

    def _pump(pipe, parts: list[str], raw: list[bytes], sink) -> None:
        nonlocal last_activity
        # backslashreplace: a byte that is not UTF-8 (diagnostics in the Windows system
        # encoding) is kept as a visible escape rather than replaced by U+FFFD — the
        # exact evidence of the error is not lost (a finding of round 9: "verbatim"
        # covers broken bytes too).
        decoder = codecs.getincrementaldecoder("utf-8")(errors="backslashreplace")
        # Corruption is detected by an INCREMENTAL strict decoder: it buffers a
        # multi-byte character cut in half by a read boundary and so gives no false raw
        # evidence on valid Cyrillic (round 18: a one-shot chunk.decode() counted the cut
        # as corruption).
        strict = codecs.getincrementaldecoder("utf-8")()
        fd = pipe.fileno()
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                parts.append(decoder.decode(b"", final=True))
                try:
                    strict.decode(b"", True)  # a truncated tail — corruption
                except UnicodeDecodeError:
                    had_invalid[0] = True
                return
            try:
                strict.decode(chunk)
            except UnicodeDecodeError:
                had_invalid[0] = True
                strict.reset()
            with lock:
                raw.append(chunk)
            text = decoder.decode(chunk)
            with lock:
                parts.append(text)
                last_activity = time.monotonic()
            # The write happens OUTSIDE the lock: a hung write must not block the monitor
            # (whose job is to measure silence and kill on the window).
            if sink is not None and text and not sink_error:
                try:
                    sink.write(text)
                    sink.flush()
                except ValueError:
                    # The sink is already closed (the role finished, a late chunk): the text is
                    # intact in parts and will reach the caller — not a refusal.
                    return
                except OSError as exc:
                    sink_error.append(exc)

    try:
        stream_cm = open(stream_path, "w", encoding="utf-8")
    except OSError as exc:
        killed = tree.kill()
        raise HarnessError(
            f"Could not open the stream file of role \"{role}\": {stream_path}",
            cause=(
                f"the OS refused the write: {exc}; "
                + ("the process was killed with its tree" if killed else "the death of the tree is NOT confirmed")
            ),
            next_action=(
                "check the stream directory (permissions, free space) and restart the round"
                + ("" if killed else "; find and finish off the role's processes by hand")
            ),
        ) from exc
    # ANY refusal after the role has started must leave a dead tree behind it: a
    # privacy refusal, a disk problem, anything else — kill first, raise second (a
    # finding of round 8: the privacy refusal went upwards leaving the role and its
    # DEVNULL helper alive). The body is a closure so that one try covers every exit
    # from it.
    def _body() -> tuple[str, str]:
        if input_text is not None:
            def _feed() -> None:
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(input_text.encode("utf-8"))
                    proc.stdin.close()
                except OSError:
                    pass  # the role closed stdin — its right

            threading.Thread(target=_feed, daemon=True).start()
        with stream_cm as stream:
            # Both streams go into partial: on a timeout or a crash the header and the
            # diagnostics from stderr must be intact on disk, as the refusal promises (a
            # finding of round 6). The order inside partial is the order of arrival; the
            # final transcript is assembled by the caller (stderr-first).
            threads = [
                threading.Thread(
                    target=_pump,
                    args=(proc.stdout, out_parts, raw_out, stream),
                    daemon=True,
                ),
                # stderr is a sign of life too: a role writing diagnostics is alive.
                threading.Thread(
                    target=_pump,
                    args=(proc.stderr, err_parts, raw_err, stream),
                    daemon=True,
                ),
            ]
            for t in threads:
                t.start()

            while proc.poll() is None:
                time.sleep(1)
                if sink_error:
                    tree.kill()
                    raise HarnessError(
                        f"The stream of role \"{role}\" failed mid-work",
                        cause=f"a disk write error on {stream_path}: {sink_error[0]}",
                        next_action="check the space and the permissions; the role was killed with its tree, restart the round",
                    )
                if privacy_check is not None:
                    # Privacy is checked on EVERY tick of the stream: a directory that became
                    # tracked in the middle of a role (development edits .gitignore as a matter
                    # of course) stops the role within the tick rather than being discovered
                    # after the leak.
                    privacy_check()
                with lock:
                    silence = time.monotonic() - last_activity
                if silence > detection_window_sec:
                    confirmed = tree.kill()
                    raise HarnessError(
                        f"Role \"{role}\" is judged dead: silence longer than the recognition window",
                        cause=(
                            f"no output for {int(silence)}s against a window of {detection_window_sec}s "
                            "(the window is grounded in the observed stalls of this environment)"
                            + ("" if confirmed else "; the death of the tree is NOT confirmed")
                        ),
                        next_action=(
                            f"the part of the output already received is intact in {stream_path}; restart the round; "
                            "if the environment got worse, refresh the observed stall in the machine profile"
                            + (
                                ""
                                if confirmed
                                else "; check for and finish off the role's orphaned processes by hand"
                            )
                        ),
                    )
            # The parent has exited. A role is finished only together with its tree — we
            # finish off the surviving descendants UNCONDITIONALLY (the invisible ones
            # included: a helper on DEVNULL pipes holds none of our streams and would
            # otherwise outlive the role's "success"). An unconfirmed death is a refusal on
            # the successful path as well.
            if not tree.reap_survivors():
                raise HarnessError(
                    f"Role \"{role}\" finished, but the death of its tree is not confirmed",
                    cause="closing the Job Object / process group did not confirm the finish-off",
                    next_action=(
                        f"the output received is intact in {stream_path}; find and end the role's "
                        "orphaned processes, then restart the round"
                    ),
                )
            for t in threads:
                t.join(timeout=10)
            if sink_error:
                raise HarnessError(
                    f"The stream of role \"{role}\" failed mid-work",
                    cause=f"a disk write error on {stream_path}: {sink_error[0]}",
                    next_action="check the space and the permissions on the run catalogue; restart the round",
                )
            if any(t.is_alive() for t in threads):
                raise HarnessError(
                    f"Role \"{role}\" finished, but its output did not close",
                    cause=(
                        "the pipe-reading threads are alive after the tree was finished off — "
                        "an unaccounted holder of the pipes"
                    ),
                    next_action=(
                        f"the part of the output already received is intact in {stream_path}; find and end the "
                        "processes holding the role's pipes, then restart the round"
                    ),
                )

        if proc.returncode != 0:
            stderr = "".join(err_parts).strip()
            raise HarnessError(
                f"Role \"{role}\" finished with an error (code {proc.returncode})",
                cause=f"stderr: {stderr[:800] or '<empty — which is itself a defect of the tool>'}",
                next_action=(
                    f"partial output in {stream_path}; work it out from stderr and restart the round — "
                    "what is written in the run catalogue is intact"
                ),
            )
        return "".join(out_parts), "".join(err_parts)

    def _dump_raw(*, loud: bool) -> None:
        if not had_invalid[0]:
            return
        try:
            for suffix, chunks in ((".stderr.raw", raw_err), (".stdout.raw", raw_out)):
                raw_path = stream_path.with_suffix(stream_path.suffix + suffix)
                with open(raw_path, "wb") as rf:
                    for c in chunks:
                        rf.write(c)
        except OSError as exc:
            if loud:
                # The promise of exact bytes is not broken silently: on the successful path a
                # failure of the raw evidence is a refusal (round 11); on the refusal path
                # (loud=False) the original refusal matters more.
                raise HarnessError(
                    f"Could not save the raw evidence: {stream_path}.stderr.raw/.stdout.raw",
                    cause=f"the role's output held non-UTF-8 and the write failed: {exc}",
                    next_action=(
                        "the textual form with escapes is intact; check the free space "
                        "and restart the round if the exact bytes matter"
                    ),
                ) from exc

    try:
        result = _body()
        _dump_raw(loud=True)
        return result
    except BaseException as exc:
        _dump_raw(loud=False)
        confirmed = tree.kill()
        if isinstance(exc, HarnessError) and not confirmed:
            # A refusal has no right to report "the role was stopped" when finishing off the
            # tree was not confirmed — that would be silence about a living process (a
            # finding of round 9).
            raise HarnessError(
                exc.what + " (the death of the tree is NOT confirmed)",
                cause=exc.cause,
                next_action=exc.next_action + "; find and finish off the role's processes by hand",
            ) from exc
        raise
