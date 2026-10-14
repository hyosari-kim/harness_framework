"""
execute.py 리팩터링 안전망 테스트.
리팩터링 전후 동작이 동일한지 검증한다.
"""

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """phases/, CLAUDE.md, docs/ 를 갖춘 임시 프로젝트 구조."""
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Rules\n- rule one\n- rule two")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "arch.md").write_text("# Architecture\nSome content")
    (docs_dir / "guide.md").write_text("# Guide\nAnother doc")

    return tmp_path


@pytest.fixture
def phase_dir(tmp_project):
    """step 3개를 가진 phase 디렉토리."""
    d = tmp_project / "phases" / "0-mvp"
    d.mkdir()

    index = {
        "project": "TestProject",
        "phase": "mvp",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "프로젝트 초기화 완료"},
            {"step": 1, "name": "core", "status": "completed", "summary": "핵심 로직 구현"},
            {"step": 2, "name": "ui", "status": "pending"},
        ],
    }
    (d / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    (d / "step2.md").write_text("# Step 2: UI\n\nUI를 구현하세요.")

    return d


@pytest.fixture
def top_index(tmp_project):
    """phases/index.json (top-level)."""
    top = {
        "phases": [
            {"dir": "0-mvp", "status": "pending"},
            {"dir": "1-polish", "status": "pending"},
        ]
    }
    p = tmp_project / "phases" / "index.json"
    p.write_text(json.dumps(top, indent=2))
    return p


@pytest.fixture
def executor(tmp_project, phase_dir):
    """테스트용 StepExecutor 인스턴스. git 호출은 별도 mock 필요."""
    with patch.object(ex, "ROOT", tmp_project):
        inst = ex.StepExecutor("0-mvp")
    # 내부 경로를 tmp_project 기준으로 재설정
    inst._root = str(tmp_project)
    inst._phases_dir = tmp_project / "phases"
    inst._phase_dir = phase_dir
    inst._phase_dir_name = "0-mvp"
    inst._index_file = phase_dir / "index.json"
    inst._top_index_file = tmp_project / "phases" / "index.json"
    return inst


# ---------------------------------------------------------------------------
# _stamp (= 이전 now_iso)
# ---------------------------------------------------------------------------

class TestStamp:
    def test_returns_kst_timestamp(self, executor):
        result = executor._stamp()
        assert "+0900" in result

    def test_format_is_iso(self, executor):
        result = executor._stamp()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert dt.tzinfo is not None

    def test_is_current_time(self, executor):
        before = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0)
        result = executor._stamp()
        after = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0) + timedelta(seconds=1)
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert before <= parsed <= after


# ---------------------------------------------------------------------------
# _read_json / _write_json
# ---------------------------------------------------------------------------

class TestJsonHelpers:
    def test_roundtrip(self, tmp_path):
        data = {"key": "값", "nested": [1, 2, 3]}
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, data)
        loaded = ex.StepExecutor._read_json(p)
        assert loaded == data

    def test_save_ensures_ascii_false(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"한글": "테스트"})
        raw = p.read_text()
        assert "한글" in raw
        assert "\\u" not in raw

    def test_save_indented(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"a": 1})
        raw = p.read_text()
        assert "\n" in raw

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ex.StepExecutor._read_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# _load_guardrails
# ---------------------------------------------------------------------------

class TestLoadGuardrails:
    def test_loads_claude_md_and_docs(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "# Rules" in result
        assert "rule one" in result
        assert "# Architecture" in result
        assert "# Guide" in result

    def test_sections_separated_by_divider(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "---" in result

    def test_docs_sorted_alphabetically(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        arch_pos = result.index("arch")
        guide_pos = result.index("guide")
        assert arch_pos < guide_pos

    def test_no_claude_md(self, executor, tmp_project):
        (tmp_project / "CLAUDE.md").unlink()
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "CLAUDE.md" not in result
        assert "Architecture" in result

    def test_no_docs_dir(self, executor, tmp_project):
        import shutil
        shutil.rmtree(tmp_project / "docs")
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "Rules" in result
        assert "Architecture" not in result

    def test_empty_project(self, tmp_path):
        with patch.object(ex, "ROOT", tmp_path):
            # executor가 필요 없는 static-like 동작이므로 임시 인스턴스
            phases_dir = tmp_path / "phases" / "dummy"
            phases_dir.mkdir(parents=True)
            idx = {"project": "T", "phase": "t", "steps": []}
            (phases_dir / "index.json").write_text(json.dumps(idx))
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
            result = inst._load_guardrails()
        assert result == ""


# ---------------------------------------------------------------------------
# _build_step_context
# ---------------------------------------------------------------------------

class TestBuildStepContext:
    def test_includes_completed_with_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "Step 0 (setup): 프로젝트 초기화 완료" in result
        assert "Step 1 (core): 핵심 로직 구현" in result

    def test_excludes_pending(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "ui" not in result

    def test_excludes_completed_without_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        del index["steps"][0]["summary"]
        result = ex.StepExecutor._build_step_context(index)
        assert "setup" not in result
        assert "core" in result

    def test_empty_when_no_completed(self):
        index = {"steps": [{"step": 0, "name": "a", "status": "pending"}]}
        result = ex.StepExecutor._build_step_context(index)
        assert result == ""

    def test_has_header(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert result.startswith("## 이전 Step 산출물")


# ---------------------------------------------------------------------------
# _build_preamble
# ---------------------------------------------------------------------------

class TestBuildPreamble:
    def test_includes_project_name(self, executor):
        result = executor._build_preamble("", "")
        assert "TestProject" in result

    def test_includes_guardrails(self, executor):
        result = executor._build_preamble("GUARD_CONTENT", "")
        assert "GUARD_CONTENT" in result

    def test_includes_step_context(self, executor):
        ctx = "## 이전 Step 산출물\n\n- Step 0: done"
        result = executor._build_preamble("", ctx)
        assert "이전 Step 산출물" in result

    def test_includes_commit_example(self, executor):
        result = executor._build_preamble("", "")
        assert "feat(mvp):" in result

    def test_includes_rules(self, executor):
        result = executor._build_preamble("", "")
        assert "작업 규칙" in result
        assert "AC" in result

    def test_no_retry_section_by_default(self, executor):
        result = executor._build_preamble("", "")
        assert "이전 시도 실패" not in result

    def test_retry_section_with_prev_error(self, executor):
        result = executor._build_preamble("", "", prev_error="타입 에러 발생")
        assert "이전 시도 실패" in result
        assert "타입 에러 발생" in result

    def test_includes_max_retries(self, executor):
        result = executor._build_preamble("", "")
        assert str(ex.StepExecutor.MAX_RETRIES) in result

    def test_includes_index_path(self, executor):
        result = executor._build_preamble("", "")
        assert "/phases/0-mvp/index.json" in result


# ---------------------------------------------------------------------------
# _update_top_index
# ---------------------------------------------------------------------------

class TestUpdateTopIndex:
    def test_completed(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "completed"
        assert "completed_at" in mvp

    def test_error(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("error")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "error"
        assert "failed_at" in mvp

    def test_blocked(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("blocked")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "blocked"
        assert "blocked_at" in mvp

    def test_other_phases_unchanged(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        polish = next(p for p in data["phases"] if p["dir"] == "1-polish")
        assert polish["status"] == "pending"

    def test_nonexistent_dir_is_noop(self, executor, top_index):
        executor._top_index_file = top_index
        executor._phase_dir_name = "no-such-dir"
        original = json.loads(top_index.read_text())
        executor._update_top_index("completed")
        after = json.loads(top_index.read_text())
        for p_before, p_after in zip(original["phases"], after["phases"]):
            assert p_before["status"] == p_after["status"]

    def test_no_top_index_file(self, executor, tmp_path):
        executor._top_index_file = tmp_path / "nonexistent.json"
        executor._update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# _checkout_branch (mocked)
# ---------------------------------------------------------------------------

class TestCheckoutBranch:
    def _mock_git(self, executor, responses):
        call_idx = {"i": 0}
        def fake_git(*args):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(responses):
                return responses[idx]
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

    def test_already_on_branch(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="feat-mvp\n", stderr=""),
        ])
        executor._checkout_branch()  # should return without checkout

    def test_branch_exists_checkout(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_checkout_fails_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="dirty tree"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1

    def test_no_git_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=1, stdout="", stderr="not a git repo"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_step (mocked)
# ---------------------------------------------------------------------------

class TestCommitStep:
    def test_two_phase_commit(self, executor):
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_calls = [c for c in calls if c[0] == "commit"]
        assert len(commit_calls) == 2
        assert "feat(mvp):" in commit_calls[0][2]
        assert "chore(mvp):" in commit_calls[1][2]

    def test_no_code_changes_skips_feat_commit(self, executor):
        call_count = {"diff": 0}
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                call_count["diff"] += 1
                if call_count["diff"] == 1:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(commit_msgs) == 1
        assert "chore" in commit_msgs[0]


# ---------------------------------------------------------------------------
# _invoke_claude (mocked)
# ---------------------------------------------------------------------------

class TestInvokeClaude:
    def test_invokes_claude_with_correct_args(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")
        step = {"step": 2, "name": "ui"}
        preamble = "PREAMBLE\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            output = executor._invoke_claude(step, preamble)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert "PREAMBLE" in cmd[-1]
        assert "UI를 구현하세요" in cmd[-1]

    def test_step_sessions_run_on_sonnet(self, executor):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude({"step": 2, "name": "ui"}, "preamble")
            executor._invoke_claude({"step": 2, "name": "ui"}, "preamble", output_name="step2-test-output.json")

        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert cmd[cmd.index("--model") + 1] == "sonnet"

    def test_saves_output_json(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result):
            executor._invoke_claude(step, "preamble")

        output_file = executor._phase_dir / "step2-output.json"
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["step"] == 2
        assert data["name"] == "ui"
        assert data["exitCode"] == 0

    def test_nonexistent_step_file_exits(self, executor):
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_claude(step, "preamble")
        assert exc_info.value.code == 1

    def test_timeout_is_1800(self, executor):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude(step, "preamble")

        assert mock_run.call_args[1]["timeout"] == 1800


# ---------------------------------------------------------------------------
# progress_indicator (= 이전 Spinner)
# ---------------------------------------------------------------------------

class TestProgressIndicator:
    def test_context_manager(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1

    def test_elapsed_increases(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.2)
        assert pi.elapsed > 0


# ---------------------------------------------------------------------------
# main() CLI 파싱 (mocked)
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_no_args_exits(self):
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse exits with 2

    def test_invalid_phase_dir_exits(self):
        with patch("sys.argv", ["execute.py", "nonexistent"]):
            with patch.object(ex, "ROOT", Path("/tmp/fake_nonexistent")):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "phases" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]):
            with patch.object(ex, "ROOT", tmp_project):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _check_blockers (= 이전 main() error/blocked 체크)
# ---------------------------------------------------------------------------

class TestCheckBlockers:
    def _make_executor_with_steps(self, tmp_project, steps):
        d = tmp_project / "phases" / "test-phase"
        d.mkdir(exist_ok=True)
        index = {"project": "T", "phase": "test", "steps": steps}
        (d / "index.json").write_text(json.dumps(index))

        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
        inst._root = str(tmp_project)
        inst._phases_dir = tmp_project / "phases"
        inst._phase_dir = d
        inst._phase_dir_name = "test-phase"
        inst._index_file = d / "index.json"
        inst._top_index_file = tmp_project / "phases" / "index.json"
        inst._phase_name = "test"
        inst._total = len(steps)
        return inst

    def test_error_step_exits_1(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# --review: phase 완료 뒤 /review(규칙) → /logic-review(로직) 실행
# ---------------------------------------------------------------------------

def _claude_result(text, returncode=0):
    return MagicMock(returncode=returncode, stdout=json.dumps({"result": text}), stderr="")


class TestRunReviews:
    def test_runs_review_then_logic_review(self, executor):
        with patch("subprocess.run", side_effect=[_claude_result("VERDICT: PASS"), _claude_result("버그 없음")]) as mock_run:
            executor._run_reviews()

        prompts = [c[0][0][-1] for c in mock_run.call_args_list]
        assert prompts == ["/review", "/logic-review"]
        cmd = mock_run.call_args_list[0][0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd

    def test_review_sessions_are_restricted(self, executor):
        with patch("subprocess.run", side_effect=[_claude_result("VERDICT: PASS"), _claude_result("")]) as mock_run:
            executor._run_reviews()

        for call in mock_run.call_args_list:
            cmd = call[0][0]
            arg = lambda flag: cmd[cmd.index(flag) + 1]
            assert "--dangerously-skip-permissions" not in cmd
            # 쓸 수 있는 내장 도구 자체를 읽기와 Bash로 줄인다(Edit·Write 없음)
            assert set(arg("--tools").split(",")) == {"Read", "Grep", "Glob", "Bash"}
            # Bash는 허용 목록만 확인 없이 실행하고, 나머지는 묻지 않고 거부한다
            allowed = arg("--allowedTools").split(",")
            assert "Bash(git diff:*)" in allowed and "Bash(npm run:*)" in allowed
            assert not any(t.startswith("Bash(git push") or t == "Bash" for t in allowed)
            assert arg("--permission-mode") == "dontAsk"
            # 사용자 설정의 넓은 허용(Bash(*), Edit 등)과 auto 모드를 들이지 않는다
            assert arg("--setting-sources") == "project"

    def test_saves_outputs(self, executor):
        with patch("subprocess.run", side_effect=[_claude_result("✅ 통과"), _claude_result("지적 1")]):
            executor._run_reviews()

        review = json.loads((executor._phase_dir / "review-output.json").read_text())
        logic = json.loads((executor._phase_dir / "logic-review-output.json").read_text())
        assert review["command"] == "/review"
        assert review["exitCode"] == 0
        assert review["result"] == "✅ 통과"
        assert logic["result"] == "지적 1"

    def test_not_blocked_when_verdict_pass(self, executor):
        text = "| 아키텍처 | ✅ |\n비고: ❌ 항목 없음\n\nVERDICT: PASS"
        with patch("subprocess.run", side_effect=[_claude_result(text), _claude_result("버그 의심")]):
            assert executor._run_reviews() is False

    def test_blocked_when_verdict_fail(self, executor):
        with patch("subprocess.run", side_effect=[_claude_result("| 테스트 | ❌ |\nVERDICT: FAIL"), _claude_result("")]):
            assert executor._run_reviews() is True

    def test_blocked_when_verdict_missing(self, executor):
        with patch("subprocess.run", side_effect=[_claude_result("| 테스트 | ✅ |"), _claude_result("")]):
            assert executor._run_reviews() is True

    def test_last_verdict_line_wins(self, executor):
        text = "예시: VERDICT: FAIL\n...\nVERDICT: PASS\n"
        with patch("subprocess.run", side_effect=[_claude_result(text), _claude_result("")]):
            assert executor._run_reviews() is False

    def test_blocked_when_review_fails_to_run(self, executor):
        with patch("subprocess.run", side_effect=[_claude_result("", returncode=1), _claude_result("")]):
            assert executor._run_reviews() is True

    def test_blocked_when_review_output_not_json(self, executor):
        bad = MagicMock(returncode=0, stdout="not json", stderr="")
        with patch("subprocess.run", side_effect=[bad, _claude_result("")]):
            assert executor._run_reviews() is True


class TestFinalizeReview:
    def _prepare(self, executor, top_index, *, review, push):
        executor._auto_review = review
        executor._auto_push = push
        executor._phase_name = "mvp"
        git_calls = []

        def fake_git(*args):
            git_calls.append(args)
            # diff --cached --quiet: 1이면 커밋할 변경이 있다
            return MagicMock(returncode=1 if args[:1] == ("diff",) else 0, stdout="", stderr="")

        executor._run_git = fake_git
        return git_calls

    def test_no_review_by_default(self, executor, top_index):
        self._prepare(executor, top_index, review=False, push=False)
        with patch.object(executor, "_run_reviews") as runs:
            executor._finalize()
        runs.assert_not_called()

    def test_runs_reviews_after_phase_commit_and_commits_outputs(self, executor, top_index):
        git_calls = self._prepare(executor, top_index, review=True, push=False)
        order = []
        with patch.object(executor, "_run_reviews", side_effect=lambda: order.append(len(git_calls)) or False):
            executor._finalize()
        commits = [c for c in git_calls if c[0] == "commit"]
        assert commits[0][-1] == "chore(mvp): mark phase completed"
        assert commits[-1][-1] == "chore(mvp): review output"
        # 리뷰는 phase 완료 커밋 뒤에 돈다
        first_commit_idx = git_calls.index(commits[0])
        assert order[0] > first_commit_idx

    def test_pushes_when_not_blocked(self, executor, top_index):
        git_calls = self._prepare(executor, top_index, review=True, push=True)
        with patch.object(executor, "_run_reviews", return_value=False):
            executor._finalize()
        assert any(c[0] == "push" for c in git_calls)

    def test_skips_push_and_exits_when_blocked(self, executor, top_index):
        git_calls = self._prepare(executor, top_index, review=True, push=True)
        with patch.object(executor, "_run_reviews", return_value=True):
            with pytest.raises(SystemExit) as exc_info:
                executor._finalize()
        assert exc_info.value.code == 1
        assert not any(c[0] == "push" for c in git_calls)


class TestMainCliReview:
    def test_review_flag_passed_to_executor(self):
        with patch("sys.argv", ["execute.py", "0-mvp", "--review", "--push"]):
            with patch.object(ex, "StepExecutor") as cls:
                ex.main()
        cls.assert_called_once_with("0-mvp", auto_push=True, auto_review=True)

    def test_review_off_by_default(self):
        with patch("sys.argv", ["execute.py", "0-mvp"]):
            with patch.object(ex, "StepExecutor") as cls:
                ex.main()
        cls.assert_called_once_with("0-mvp", auto_push=False, auto_review=False)


# ---------------------------------------------------------------------------
# step을 테스트 세션 → 구현 세션으로 나눠 실행
# ---------------------------------------------------------------------------

@pytest.fixture
def git_executor(executor, tmp_project):
    """tmp_project를 실제 git 저장소로 만든 executor (테스트 파일 커밋·대조 확인용)."""
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_project, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_project / "src").mkdir()
    (tmp_project / "src" / "a.ts").write_text("export const a = 1;\n")
    (tmp_project / "src" / "a.test.ts").write_text("it('a', () => {});\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return executor


def _head(root):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()


def _step(executor, num=2):
    return next(s for s in executor._read_json(executor._index_file)["steps"] if s["step"] == num)


def _update_step(executor, num=2, **fields):
    index = executor._read_json(executor._index_file)
    for s in index["steps"]:
        if s["step"] == num:
            s.update(fields)
    executor._write_json(executor._index_file, index)


class TestIsTestFile:
    @pytest.mark.parametrize("path", ["src/a.test.ts", "src/x/B.test.tsx", "test/screens/a.test.tsx", "test/sqliteTestDb.ts"])
    def test_test_files(self, path):
        assert ex.StepExecutor._is_test_file(path)

    @pytest.mark.parametrize("path", ["src/a.ts", "src/test.ts", "docs/ADR.md", "src/a.testing.ts"])
    def test_non_test_files(self, path):
        assert not ex.StepExecutor._is_test_file(path)


class TestWriteTests:
    def test_commits_only_test_files_and_records_commit(self, git_executor, tmp_project):
        def session(step, preamble, output_name=None):
            (tmp_project / "src" / "b.test.ts").write_text("it('b', () => {});\n")
            return {}

        with patch.object(git_executor, "_invoke_claude", side_effect=session) as inv:
            git_executor._write_tests({"step": 2, "name": "ui"}, "GUARD")

        assert inv.call_args[1]["output_name"] == "step2-test-output.json"
        log = subprocess.run(["git", "log", "-1", "--format=%s", "--name-only"], cwd=tmp_project,
                             capture_output=True, text=True).stdout.split()
        assert " ".join(log[:5]) == "test(mvp): step 2 — ui"
        assert log[5:] == ["src/b.test.ts"]
        s = _step(git_executor)
        assert s["tests_commit"] == _head(tmp_project)
        assert s["status"] == "pending"

    def test_preamble_limits_session_to_tests(self, git_executor, tmp_project):
        def session(step, preamble, output_name=None):
            (tmp_project / "src" / "b.test.ts").write_text("x")
            return {}

        with patch.object(git_executor, "_invoke_claude", side_effect=session) as inv:
            git_executor._write_tests({"step": 2, "name": "ui"}, "GUARD")

        preamble = inv.call_args[0][1]
        assert "GUARD" in preamble
        assert "테스트만" in preamble
        assert "커밋하지 마라" in preamble

    def test_retries_when_non_test_file_changed(self, git_executor, tmp_project):
        calls = []

        def session(step, preamble, output_name=None):
            calls.append(preamble)
            if len(calls) == 1:
                (tmp_project / "src" / "a.ts").write_text("export const a = 2;\n")
            else:
                (tmp_project / "src" / "a.ts").write_text("export const a = 1;\n")
                (tmp_project / "src" / "b.test.ts").write_text("x")
            return {}

        with patch.object(git_executor, "_invoke_claude", side_effect=session):
            git_executor._write_tests({"step": 2, "name": "ui"}, "")

        assert len(calls) == 2
        assert "src/a.ts" in calls[1]
        assert _step(git_executor)["tests_commit"] == _head(tmp_project)

    def test_retries_when_no_test_written(self, git_executor, tmp_project):
        calls = []

        def session(step, preamble, output_name=None):
            calls.append(preamble)
            if len(calls) == 2:
                (tmp_project / "src" / "b.test.ts").write_text("x")
            return {}

        with patch.object(git_executor, "_invoke_claude", side_effect=session):
            git_executor._write_tests({"step": 2, "name": "ui"}, "")

        assert len(calls) == 2
        assert "테스트 파일을 쓰지 않았" in calls[1]

    def test_error_after_max_retries(self, git_executor, tmp_project, top_index):
        with patch.object(git_executor, "_invoke_claude", return_value={}):
            with pytest.raises(SystemExit) as exc_info:
                git_executor._write_tests({"step": 2, "name": "ui"}, "")

        assert exc_info.value.code == 1
        s = _step(git_executor)
        assert s["status"] == "error"
        assert "테스트 파일을 쓰지 않았" in s["error_message"]
        assert "tests_commit" not in s

    def test_blocked_exits(self, git_executor, top_index):
        def session(step, preamble, output_name=None):
            _update_step(git_executor, status="blocked", blocked_reason="API 키 필요")
            return {}

        with patch.object(git_executor, "_invoke_claude", side_effect=session):
            with pytest.raises(SystemExit) as exc_info:
                git_executor._write_tests({"step": 2, "name": "ui"}, "")

        assert exc_info.value.code == 2
        assert "blocked_at" in _step(git_executor)


SKIP_ENTRY = {
    "test": "src/a.test.ts › a",
    "step_doc": "step2 §1: 빈 값이면 null을 돌려준다",
    "test_expects": "빈 값이면 던진다",
    "reason": "step 문서는 null을 돌려준다고 정했는데 테스트는 예외를 기대한다",
}


class TestCheckTests:
    @pytest.fixture
    def sha(self, git_executor, tmp_project):
        return _head(tmp_project)

    def _check(self, executor, sha, skipped=None):
        s = {"step": 2, "tests_commit": sha}
        if skipped is not None:
            s["skipped_tests"] = skipped
        return executor._check_tests(s)

    def test_ok_when_tests_untouched(self, git_executor, tmp_project, sha):
        (tmp_project / "src" / "a.ts").write_text("export const a = 3;\n")
        assert self._check(git_executor, sha) is None

    def test_ok_when_skipped_with_full_entry(self, git_executor, tmp_project, sha):
        (tmp_project / "src" / "a.test.ts").write_text("it.skip('a', () => {});\n")
        assert self._check(git_executor, sha, [SKIP_ENTRY]) is None

    def test_ok_when_new_test_file_added(self, git_executor, tmp_project, sha):
        (tmp_project / "src" / "c.test.ts").write_text("it('c', () => {});\n")
        assert self._check(git_executor, sha) is None

    def test_fails_when_skip_not_reported(self, git_executor, tmp_project, sha):
        (tmp_project / "src" / "a.test.ts").write_text("it.skip('a', () => {});\n")
        assert "skipped_tests" in self._check(git_executor, sha)

    @pytest.mark.parametrize("missing", ["test", "step_doc", "test_expects", "reason"])
    def test_fails_when_entry_field_missing(self, git_executor, tmp_project, sha, missing):
        (tmp_project / "src" / "a.test.ts").write_text("it.skip('a', () => {});\n")
        entry = {**SKIP_ENTRY, missing: ""}
        assert missing in self._check(git_executor, sha, [entry])

    def test_fails_when_test_changed_otherwise(self, git_executor, tmp_project, sha):
        (tmp_project / "src" / "a.test.ts").write_text("it('a', () => { expect(1).toBe(1); });\n")
        problem = self._check(git_executor, sha)
        assert "src/a.test.ts" in problem
        assert sha in problem

    def test_fails_when_test_deleted(self, git_executor, tmp_project, sha):
        (tmp_project / "src" / "a.test.ts").unlink()
        assert "src/a.test.ts" in self._check(git_executor, sha)

    def test_sees_committed_changes(self, git_executor, tmp_project, sha):
        (tmp_project / "src" / "a.test.ts").write_text("it('a', () => { throw 1; });\n")
        subprocess.run(["git", "commit", "-qam", "feat"], cwd=tmp_project, check=True)
        assert "src/a.test.ts" in self._check(git_executor, sha)


class TestImplPreamble:
    def test_mentions_test_commit_and_skip_rules(self, executor):
        p = executor._build_preamble("", "", tests_commit="abc123")
        assert "abc123" in p
        assert ".skip" in p
        for field in ("test", "step_doc", "test_expects", "reason"):
            assert f"`{field}`" in p

    def test_no_test_section_without_test_commit(self, executor):
        p = executor._build_preamble("", "")
        assert "skipped_tests" not in p


class TestExecuteSingleStepSplit:
    def _run(self, executor, step, *, check=None, impl=None):
        sessions = []

        def write_tests(st, guardrails):
            sessions.append("tests")
            _update_step(executor, tests_commit="sha1")

        def invoke(st, preamble, output_name=None):
            sessions.append("impl")
            (impl or (lambda: _update_step(executor, status="completed", summary="done")))()
            return {}

        with patch.object(executor, "_write_tests", side_effect=write_tests), \
             patch.object(executor, "_invoke_claude", side_effect=invoke), \
             patch.object(executor, "_check_tests", side_effect=check or (lambda s: None)), \
             patch.object(executor, "_commit_step"):
            result = executor._execute_single_step(step, "")
        return sessions, result

    def test_writes_tests_then_implements(self, executor):
        sessions, result = self._run(executor, _step(executor))
        assert sessions == ["tests", "impl"]
        assert result is True

    def test_skips_test_session_when_tests_false(self, executor):
        _update_step(executor, tests=False)
        sessions, _ = self._run(executor, _step(executor))
        assert sessions == ["impl"]

    def test_skips_test_session_when_already_committed(self, executor):
        _update_step(executor, tests_commit="sha0")
        sessions, _ = self._run(executor, _step(executor))
        assert sessions == ["impl"]

    def test_retries_impl_when_tests_tampered(self, executor):
        problems = iter(["테스트 파일 src/a.test.ts를 바꿨다", None])
        prompts = []
        orig = executor._build_preamble

        def spy(*a, **k):
            prompts.append(orig(*a, **k))
            return prompts[-1]

        with patch.object(executor, "_build_preamble", side_effect=spy):
            sessions, result = self._run(executor, _step(executor), check=lambda s: next(problems))

        assert sessions == ["tests", "impl", "impl"]
        assert "src/a.test.ts를 바꿨다" in prompts[1]
        assert result is True
        assert _step(executor)["status"] == "completed"


class TestReportSkipped:
    def test_prints_all_skipped_tests(self, executor, capsys):
        _update_step(executor, num=1, skipped_tests=[SKIP_ENTRY])
        _update_step(executor, num=2, skipped_tests=[{**SKIP_ENTRY, "test": "src/b.test.ts › b"}])
        executor._report_skipped()
        out = capsys.readouterr().out
        assert "2개" in out
        assert "Step 1 (core): src/a.test.ts › a" in out
        assert "Step 2 (ui): src/b.test.ts › b" in out
        for value in ("step2 §1: 빈 값이면 null을 돌려준다", "빈 값이면 던진다", "테스트는 예외를 기대한다"):
            assert value in out

    def test_silent_when_none(self, executor, capsys):
        executor._report_skipped()
        assert capsys.readouterr().out == ""

    def test_finalize_reports(self, executor, top_index):
        executor._run_git = lambda *a: MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(executor, "_report_skipped") as report:
            executor._finalize()
        report.assert_called_once()
