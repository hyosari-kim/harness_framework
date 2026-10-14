#!/usr/bin/env python3
"""
Harness Step Executor — phase 내 step을 순차 실행하고 자가 교정한다.

Usage:
    python3 scripts/execute.py <phase-dir> [--push] [--review]

각 step은 테스트 세션(테스트만 써서 test 커밋) → 구현 세션(테스트를 고치지 않고 구현) 순으로 돈다.
index.json에서 `"tests": false`인 step은 테스트 세션을 건너뛴다.

--review: phase 완료 뒤 /review(규칙 위반) → /logic-review(로직 버그)를 실행해
phases/<phase-dir>/{review,logic-review}-output.json에 남긴다. /review의 마지막 `VERDICT:` 줄이 PASS가 아니면 push하지 않는다.
"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def progress_indicator(label: str):
    """터미널 진행 표시기. with 문으로 사용하며 .elapsed 로 경과 시간을 읽는다."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    th = threading.Thread(target=_animate, daemon=True)
    th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        th.join()
        info.elapsed = time.monotonic() - t0


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    MAX_RETRIES = 3
    # step 세션 모델. 고정하지 않으면 Claude Code 기본 모델이 바뀔 때 결과와 비용이 모르게 달라진다.
    # 구현 세션: step 문서는 Sonnet이 문서만 보고 같은 코드를 쓸 수 있게 작성한다(doc-reviewer 기준).
    # 테스트 세션: 테스트 전략에서 케이스를 정하고 빠뜨리지 않아야 해서 Opus로 돌린다.
    STEP_MODEL = "sonnet"
    TEST_MODEL = "opus"
    FEAT_MSG = "feat({phase}): step {num} — {name}"
    CHORE_MSG = "chore({phase}): step {num} output"
    TEST_MSG = "test({phase}): step {num} — {name}"
    # 구현 세션이 테스트를 건너뛸 때 skipped_tests 항목마다 채워야 하는 필드
    SKIP_FIELDS = ("test", "step_doc", "test_expects", "reason")
    SKIP_RE = re.compile(r"\b(it|test|describe)\.skip\(")
    # (출력 파일 이름, 프롬프트). /code-review는 claude -p에서 리뷰를 하지 않아 프로젝트 커맨드를 쓴다.
    REVIEWS = (("review", "/review"), ("logic-review", "/logic-review"))
    # 리뷰 세션은 권한 확인을 끄지 않는다. 쓸 수 있는 도구를 읽기와 Bash로 줄이고(--tools),
    # Bash는 git 조회·npm 검증 명령만 허용하며 나머지는 묻지 않고 거부한다(dontAsk).
    # 사용자 설정의 넓은 허용(Bash(*), Edit 등)이 들어오지 않게 프로젝트 설정만 읽는다.
    REVIEW_TOOLS = ("Read", "Grep", "Glob", "Bash")
    REVIEW_ALLOWED = (
        "Read", "Grep", "Glob",
        "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)", "Bash(git show:*)",
        "Bash(npm run:*)",
    )
    TZ = timezone(timedelta(hours=9))

    def __init__(self, phase_dir_name: str, *, auto_push: bool = False, auto_review: bool = False):
        self._root = str(ROOT)
        self._phases_dir = ROOT / "phases"
        self._phase_dir = self._phases_dir / phase_dir_name
        self._phase_dir_name = phase_dir_name
        self._top_index_file = self._phases_dir / "index.json"
        self._auto_push = auto_push
        self._auto_review = auto_review

        if not self._phase_dir.is_dir():
            print(f"ERROR: {self._phase_dir} not found")
            sys.exit(1)

        self._index_file = self._phase_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        idx = self._read_json(self._index_file)
        self._project = idx.get("project", "project")
        self._phase_name = idx.get("phase", phase_dir_name)
        self._total = len(idx["steps"])

    def run(self):
        self._print_header()
        self._check_blockers()
        self._checkout_branch()
        guardrails = self._load_guardrails()
        self._ensure_created_at()
        self._execute_all_steps(guardrails)
        self._finalize()

    # --- timestamps ---

    def _stamp(self) -> str:
        return datetime.now(self.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    # --- JSON I/O ---

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(p: Path, data: dict):
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- git ---

    def _run_git(self, *args) -> subprocess.CompletedProcess:
        cmd = ["git"] + list(args)
        return subprocess.run(cmd, cwd=self._root, capture_output=True, text=True)

    def _checkout_branch(self):
        branch = f"feat-{self._phase_name}"

        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print(f"  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        if r.stdout.strip() == branch:
            return

        r = self._run_git("rev-parse", "--verify", branch)
        r = self._run_git("checkout", branch) if r.returncode == 0 else self._run_git("checkout", "-b", branch)

        if r.returncode != 0:
            print(f"  ERROR: 브랜치 '{branch}' checkout 실패.")
            print(f"  {r.stderr.strip()}")
            print(f"  Hint: 변경사항을 stash하거나 commit한 후 다시 시도하세요.")
            sys.exit(1)

        print(f"  Branch: {branch}")

    def _commit_step(self, step_num: int, step_name: str):
        output_rel = f"phases/{self._phase_dir_name}/step{step_num}-output.json"
        index_rel = f"phases/{self._phase_dir_name}/index.json"

        self._run_git("add", "-A")
        self._run_git("reset", "HEAD", "--", output_rel)
        self._run_git("reset", "HEAD", "--", index_rel)

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.FEAT_MSG.format(phase=self._phase_name, num=step_num, name=step_name)
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  Commit: {msg}")
            else:
                print(f"  WARN: 코드 커밋 실패: {r.stderr.strip()}")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.CHORE_MSG.format(phase=self._phase_name, num=step_num)
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                print(f"  WARN: housekeeping 커밋 실패: {r.stderr.strip()}")

    # --- top-level index ---

    def _update_top_index(self, status: str):
        if not self._top_index_file.exists():
            return
        top = self._read_json(self._top_index_file)
        ts = self._stamp()
        for phase in top.get("phases", []):
            if phase.get("dir") == self._phase_dir_name:
                phase["status"] = status
                ts_key = {"completed": "completed_at", "error": "failed_at", "blocked": "blocked_at"}.get(status)
                if ts_key:
                    phase[ts_key] = ts
                break
        self._write_json(self._top_index_file, top)

    # --- guardrails & context ---

    def _load_guardrails(self) -> str:
        sections = []
        claude_md = ROOT / "CLAUDE.md"
        if claude_md.exists():
            sections.append(f"## 프로젝트 규칙 (CLAUDE.md)\n\n{claude_md.read_text()}")
        docs_dir = ROOT / "docs"
        if docs_dir.is_dir():
            for doc in sorted(docs_dir.glob("*.md")):
                sections.append(f"## {doc.stem}\n\n{doc.read_text()}")
        return "\n\n---\n\n".join(sections) if sections else ""

    @staticmethod
    def _build_step_context(index: dict) -> str:
        lines = [
            f"- Step {s['step']} ({s['name']}): {s['summary']}"
            for s in index["steps"]
            if s["status"] == "completed" and s.get("summary")
        ]
        if not lines:
            return ""
        return "## 이전 Step 산출물\n\n" + "\n".join(lines) + "\n\n"

    @staticmethod
    def _retry_section(prev_error: Optional[str]) -> str:
        if not prev_error:
            return ""
        return (
            f"\n## ⚠ 이전 시도 실패 — 아래 에러를 반드시 참고하여 수정하라\n\n"
            f"{prev_error}\n\n---\n\n"
        )

    def _build_test_preamble(self, guardrails: str, step_context: str,
                             prev_error: Optional[str] = None) -> str:
        return (
            f"당신은 {self._project} 프로젝트의 테스트 작성자입니다. 아래 step의 테스트만 쓰세요. "
            f"구현은 다음 세션이 이 테스트를 보고 합니다.\n\n"
            f"{guardrails}\n\n---\n\n"
            f"{step_context}{self._retry_section(prev_error)}"
            f"## 작업 규칙\n\n"
            f"1. 아래 step 문서 `## 작업`의 시그니처·로직이 말한 동작을 확인하는 테스트를 써라. 구현을 짐작해 맞추지 마라.\n"
            f"   `## 테스트` 절의 전략·추가 확인·테스트 파일 변경을 빠짐없이 반영하고, 단언하지 않는 것은 단언하지 마라.\n"
            f"2. 테스트 파일(`*.test.ts(x)`, `test/` 아래)만 바꾼다.\n"
            f"3. step 문서의 AC·검증 절차·status 갱신·커밋은 따르지 마라. 구현 세션과 하네스가 한다.\n"
            f"4. 사용자 개입이 필요하면 /phases/{self._phase_dir_name}/index.json 해당 step에 "
            f"\"blocked\" + \"blocked_reason\"만 적고 중단하라.\n\n---\n\n"
        )

    def _tests_section(self, tests_commit: Optional[str]) -> str:
        if not tests_commit:
            return ""
        fields = "\n".join([
            "  - `test`: `파일 › 테스트 이름`",
            "  - `step_doc`: 근거가 된 step 문서 문장(절)",
            "  - `test_expects`: 테스트가 기대하는 것",
            "  - `reason`: 둘이 어떻게 다르고, 왜 문서 쪽이 맞다고 보는지",
        ])
        return (
            f"## 이 step의 테스트\n\n"
            f"테스트는 별도 세션이 step 문서의 `## 테스트` 절대로 먼저 써서 커밋했다"
            f"(`{tests_commit}`, `git show --stat {tests_commit}`로 확인). 이 테스트가 통과하도록 구현하라.\n"
            f"- 그 테스트 파일을 고치거나 지우지 마라. 새 테스트 파일은 추가해도 된다.\n"
            f"- 기존 테스트 파일을 바꾸거나 지워야 구현할 수 있는데 그 일이 `## 테스트` 절에 없거나 테스트 세션이 빠뜨렸으면, "
            f"고치지 말고 index.json 해당 step에 \"blocked\" + \"blocked_reason\"(어느 파일을 왜 바꿔야 하는지)을 적고 즉시 중단하라.\n"
            f"- 테스트가 step 문서와 다르다고 판단될 때만 그 테스트를 `it.skip`/`test.skip`/`describe.skip`으로 바꾸고 "
            f"나머지 작업을 계속하라. 그리고 index.json 해당 step의 `skipped_tests` 배열에 건너뛴 테스트마다 아래를 모두 적어라:\n"
            f"{fields}\n"
            f"- 구현이 어렵거나 실패를 고치지 못했다는 이유로 건너뛰지 마라. 그런 경우는 error로 기록한다.\n"
            f"- 건너뛴 테스트는 phase가 끝날 때 한꺼번에 사용자에게 보고된다.\n\n---\n\n"
        )

    def _build_preamble(self, guardrails: str, step_context: str,
                        prev_error: Optional[str] = None, tests_commit: Optional[str] = None) -> str:
        commit_example = self.FEAT_MSG.format(
            phase=self._phase_name, num="N", name="<step-name>"
        )
        return (
            f"당신은 {self._project} 프로젝트의 개발자입니다. 아래 step을 수행하세요.\n\n"
            f"{guardrails}\n\n---\n\n"
            f"{step_context}{self._retry_section(prev_error)}{self._tests_section(tests_commit)}"
            f"## 작업 규칙\n\n"
            f"1. 이전 step에서 작성된 코드를 확인하고 일관성을 유지하라.\n"
            f"2. 이 step에 명시된 작업만 수행하라. 추가 기능이나 파일을 만들지 마라.\n"
            f"3. 기존 테스트를 깨뜨리지 마라.\n"
            f"4. AC(Acceptance Criteria) 검증을 직접 실행하라.\n"
            f"5. /phases/{self._phase_dir_name}/index.json의 해당 step status를 업데이트하라:\n"
            f"   - AC 통과 → \"completed\" + \"summary\" 필드에 이 step의 산출물을 한 줄로 요약\n"
            f"   - {self.MAX_RETRIES}회 수정 시도 후에도 실패 → \"error\" + \"error_message\" 기록\n"
            f"   - 사용자 개입이 필요한 경우 (API 키, 인증, 수동 설정 등) → \"blocked\" + \"blocked_reason\" 기록 후 즉시 중단\n"
            f"6. 모든 변경사항을 커밋하라:\n"
            f"   {commit_example}\n\n---\n\n"
        )

    # --- Claude 호출 ---

    def _invoke_claude(self, step: dict, preamble: str, output_name: Optional[str] = None,
                       model: Optional[str] = None) -> dict:
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        prompt = preamble + step_file.read_text()
        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", "--model", model or self.STEP_MODEL,
             "--output-format", "json", prompt],
            cwd=self._root, capture_output=True, text=True, timeout=1800,
        )

        if result.returncode != 0:
            print(f"\n  WARN: Claude가 비정상 종료됨 (code {result.returncode})")
            if result.stderr:
                print(f"  stderr: {result.stderr[:500]}")

        output = {
            "step": step_num, "name": step_name,
            "exitCode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        }
        out_path = self._phase_dir / (output_name or f"step{step_num}-output.json")
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        return output

    # --- 리뷰 ---

    def _run_reviews(self) -> bool:
        """리뷰를 차례로 실행해 결과를 남기고, push를 막아야 하면 True를 돌려준다.

        막는 기준은 /review뿐이다: 실행 실패, 결과를 읽을 수 없음, 마지막 `VERDICT:` 줄이 PASS가 아님(없음 포함).
        결과 본문의 ❌는 설명 문장에도 나올 수 있어 판정에 쓰지 않는다.
        /logic-review의 지적은 확실하지 않은 것이 섞여 있어 사람이 보고 판단한다.
        """
        blocked = False
        for name, prompt in self.REVIEWS:
            with progress_indicator(f"{prompt} 실행 중"):
                result = subprocess.run(
                    ["claude", "-p",
                     "--tools", ",".join(self.REVIEW_TOOLS),
                     "--allowedTools", ",".join(self.REVIEW_ALLOWED),
                     "--permission-mode", "dontAsk",
                     "--setting-sources", "project",
                     "--output-format", "json", prompt],
                    cwd=self._root, capture_output=True, text=True, timeout=1800,
                )
            try:
                text = json.loads(result.stdout).get("result")
            except (json.JSONDecodeError, AttributeError):
                text = None
            out_path = self._phase_dir / f"{name}-output.json"
            self._write_json(out_path, {
                "command": prompt, "exitCode": result.returncode,
                "result": text, "stderr": result.stderr,
            })
            print(f"  ✓ {prompt} → {out_path.relative_to(self._root)}")
            if name == "review" and (result.returncode != 0 or self._verdict(text) != "PASS"):
                blocked = True
        return blocked

    @staticmethod
    def _verdict(text: Optional[str]) -> Optional[str]:
        if not isinstance(text, str):
            return None
        lines = [l.strip() for l in text.splitlines() if l.strip().startswith("VERDICT:")]
        return lines[-1].removeprefix("VERDICT:").strip() if lines else None

    # --- 헤더 & 검증 ---

    def _print_header(self):
        print(f"\n{'='*60}")
        print(f"  Harness Step Executor")
        print(f"  Phase: {self._phase_name} | Steps: {self._total}")
        if self._auto_push:
            print(f"  Auto-push: enabled")
        print(f"{'='*60}")

    def _check_blockers(self):
        index = self._read_json(self._index_file)
        for s in reversed(index["steps"]):
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print(f"  Fix and reset status to 'pending' to retry.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print(f"  Resolve and reset status to 'pending' to retry.")
                sys.exit(2)
            if s["status"] != "pending":
                break

    def _ensure_created_at(self):
        index = self._read_json(self._index_file)
        if "created_at" not in index:
            index["created_at"] = self._stamp()
            self._write_json(self._index_file, index)

    # --- 테스트 세션 ---

    @staticmethod
    def _is_test_file(path: str) -> bool:
        return path.startswith("test/") or re.search(r"\.test\.[jt]sx?$", path) is not None

    def _head(self) -> str:
        return self._run_git("rev-parse", "HEAD").stdout.strip()

    def _changed_files(self, since: str) -> list:
        """since 커밋 이후 바뀐 파일(커밋·작업 트리·추적 안 된 새 파일). phases/ 메타데이터는 뺀다."""
        diff = self._run_git("diff", "--name-only", since).stdout.splitlines()
        new = self._run_git("ls-files", "--others", "--exclude-standard").stdout.splitlines()
        return sorted({f for f in diff + new if f and not f.startswith("phases/")})

    def _exit_blocked(self, index: dict, step_num: int, step_name: str, elapsed: int):
        for s in index["steps"]:
            if s["step"] == step_num:
                s["blocked_at"] = self._stamp()
        self._write_json(self._index_file, index)
        reason = next((s.get("blocked_reason", "") for s in index["steps"] if s["step"] == step_num), "")
        print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
        print(f"    Reason: {reason}")
        self._update_top_index("blocked")
        sys.exit(2)

    def _exit_error(self, index: dict, step_num: int, step_name: str, err_msg: str, elapsed: int):
        for s in index["steps"]:
            if s["step"] == step_num:
                s["status"] = "error"
                s["error_message"] = f"[{self.MAX_RETRIES}회 시도 후 실패] {err_msg}"
                s["failed_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._commit_step(step_num, step_name)
        print(f"  ✗ Step {step_num}: {step_name} failed after {self.MAX_RETRIES} attempts [{elapsed}s]")
        print(f"    Error: {err_msg}")
        self._update_top_index("error")
        sys.exit(1)

    def _write_tests(self, step: dict, guardrails: str):
        """테스트 세션: 테스트 파일만 쓰게 하고, 하네스가 그 파일만 test 커밋한 뒤 tests_commit에 기록한다."""
        step_num, step_name = step["step"], step["name"]
        prev_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            start = self._head()
            index = self._read_json(self._index_file)
            preamble = self._build_test_preamble(guardrails, self._build_step_context(index), prev_error)

            tag = f"Step {step_num}/{self._total - 1}: {step_name} [tests]"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"
            with progress_indicator(tag) as pi:
                self._invoke_claude(step, preamble, output_name=f"step{step_num}-test-output.json", model=self.TEST_MODEL)
                elapsed = int(pi.elapsed)

            index = self._read_json(self._index_file)
            status = next((s.get("status") for s in index["steps"] if s["step"] == step_num), "pending")
            if status == "blocked":
                self._exit_blocked(index, step_num, step_name, elapsed)

            changed = self._changed_files(start)
            others = [f for f in changed if not self._is_test_file(f)]
            if others:
                err_msg = f"테스트 파일이 아닌 파일을 바꿨다: {', '.join(others)}. 되돌리고 테스트 파일만 써라."
            elif not changed:
                err_msg = "테스트 파일을 쓰지 않았다."
            else:
                self._run_git("add", "-A", "--", *changed)
                self._run_git("commit", "-m", self.TEST_MSG.format(phase=self._phase_name, num=step_num, name=step_name))
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "pending"
                        s["tests_commit"] = self._head()
                self._write_json(self._index_file, index)
                print(f"  ✓ Step {step_num}: {step_name} tests [{elapsed}s]")
                return

            if attempt < self.MAX_RETRIES:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "pending"
                        s.pop("error_message", None)
                self._write_json(self._index_file, index)
                prev_error = err_msg
                print(f"  ↻ Step {step_num}: tests retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
            else:
                self._exit_error(index, step_num, step_name, err_msg, elapsed)

    def _check_tests(self, step: dict) -> Optional[str]:
        """구현 세션이 테스트 세션의 테스트를 `.skip` 말고는 바꾸지 않았고, 건너뛴 테스트를 빠짐없이 적었는지 본다.

        문제가 있으면 구현 세션에 다시 줄 메시지를, 없으면 None을 돌려준다.
        """
        sha = step["tests_commit"]
        skipped_any = False
        for path in self._changed_files(sha):
            if not self._is_test_file(path):
                continue
            original = self._run_git("show", f"{sha}:{path}")
            if original.returncode != 0:
                continue  # 구현 세션이 새로 만든 테스트 파일
            current_file = Path(self._root) / path
            if not current_file.exists():
                return f"테스트 파일 {path}를 지웠다. 되돌려라: git checkout {sha} -- {path}"
            current = current_file.read_text(encoding="utf-8")
            if self.SKIP_RE.sub(r"\1(", current) != self.SKIP_RE.sub(r"\1(", original.stdout):
                return (f"테스트 파일 {path}를 `.skip` 말고 다르게 바꿨다. "
                        f"되돌리고(git checkout {sha} -- {path}) 구현을 고쳐라.")
            skipped_any = skipped_any or current != original.stdout

        skipped = step.get("skipped_tests") or []
        if skipped_any and not skipped:
            return "테스트를 `.skip`으로 건너뛰었지만 index.json의 skipped_tests에 적지 않았다."
        for entry in skipped:
            missing = [k for k in self.SKIP_FIELDS if not (isinstance(entry, dict) and entry.get(k))]
            if missing:
                return f"skipped_tests 항목에 {', '.join(missing)}이(가) 비어 있다. 건너뛴 테스트마다 {', '.join(self.SKIP_FIELDS)}를 모두 적어라."
        return None

    # --- 실행 루프 ---

    def _execute_single_step(self, step: dict, guardrails: str) -> bool:
        """단일 step 실행 (테스트 세션 → 구현 세션, 재시도 포함). 완료되면 True, 실패/차단이면 False."""
        step_num, step_name = step["step"], step["name"]
        if step.get("tests", True) and not step.get("tests_commit"):
            self._write_tests(step, guardrails)
        tests_commit = next(s for s in self._read_json(self._index_file)["steps"] if s["step"] == step_num).get("tests_commit")

        done = sum(1 for s in self._read_json(self._index_file)["steps"] if s["status"] == "completed")
        prev_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            index = self._read_json(self._index_file)
            step_context = self._build_step_context(index)
            preamble = self._build_preamble(guardrails, step_context, prev_error, tests_commit)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            with progress_indicator(tag) as pi:
                self._invoke_claude(step, preamble)
                elapsed = int(pi.elapsed)

            index = self._read_json(self._index_file)
            current = next((s for s in index["steps"] if s["step"] == step_num), {})
            status = current.get("status", "pending")

            if status == "completed":
                err_msg = self._check_tests(current) if tests_commit else None
                if err_msg is None:
                    current["completed_at"] = self._stamp()
                    self._write_json(self._index_file, index)
                    self._commit_step(step_num, step_name)
                    print(f"  ✓ Step {step_num}: {step_name} [{elapsed}s]")
                    return True
            elif status == "blocked":
                self._exit_blocked(index, step_num, step_name, elapsed)
            else:
                err_msg = current.get("error_message", "Step did not update status")

            if attempt < self.MAX_RETRIES:
                current["status"] = "pending"
                current.pop("error_message", None)
                self._write_json(self._index_file, index)
                prev_error = err_msg
                print(f"  ↻ Step {step_num}: retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
            else:
                self._exit_error(index, step_num, step_name, err_msg, elapsed)

        return False  # unreachable

    def _report_skipped(self):
        """구현 세션이 step 문서와 다르다고 보고 건너뛴 테스트를 phase 끝에 한꺼번에 보여 준다."""
        index = self._read_json(self._index_file)
        rows = [(s, e) for s in index["steps"] for e in s.get("skipped_tests") or []]
        if not rows:
            return
        print(f"\n  ⚠ 구현 세션이 건너뛴 테스트 {len(rows)}개 (step 문서와 다르다고 판단):")
        for s, e in rows:
            print(f"    - Step {s['step']} ({s['name']}): {e.get('test')}")
            print(f"      step 문서: {e.get('step_doc')}")
            print(f"      테스트 기대: {e.get('test_expects')}")
            print(f"      이유: {e.get('reason')}")

    def _execute_all_steps(self, guardrails: str):
        while True:
            index = self._read_json(self._index_file)
            pending = next((s for s in index["steps"] if s["status"] == "pending"), None)
            if pending is None:
                print("\n  All steps completed!")
                return

            step_num = pending["step"]
            for s in index["steps"]:
                if s["step"] == step_num and "started_at" not in s:
                    s["started_at"] = self._stamp()
                    self._write_json(self._index_file, index)
                    break

            self._execute_single_step(pending, guardrails)

    def _finalize(self):
        index = self._read_json(self._index_file)
        index["completed_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._update_top_index("completed")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = f"chore({self._phase_name}): mark phase completed"
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  ✓ {msg}")

        self._report_skipped()

        if self._auto_review:
            blocked = self._run_reviews()
            self._run_git("add", "-A")
            if self._run_git("diff", "--cached", "--quiet").returncode != 0:
                self._run_git("commit", "-m", f"chore({self._phase_name}): review output")
            if blocked and self._auto_push:
                print("\n  ERROR: /review가 PASS가 아니어서 push하지 않았습니다. review-output.json을 확인하세요.")
                sys.exit(1)

        if self._auto_push:
            branch = f"feat-{self._phase_name}"
            r = self._run_git("push", "-u", "origin", branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push 실패: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{branch}")

        print(f"\n{'='*60}")
        print(f"  Phase '{self._phase_name}' completed!")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    parser.add_argument("--review", action="store_true", help="Run /review and /logic-review after completion")
    args = parser.parse_args()

    StepExecutor(args.phase_dir, auto_push=args.push, auto_review=args.review).run()


if __name__ == "__main__":
    main()
