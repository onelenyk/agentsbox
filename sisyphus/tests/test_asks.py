"""What a "Wants you" card is asking — core/asks.py."""
from core import asks


def _q(text, author="orchestrator", **extra):
    return [{"kind": "question", "author": author, "text": text}] + extra.get("more", [])


class TestAStuckTaskIsNotAQuestion:
    def test_a_runner_that_returned_nothing_is_stuck(self):
        a = asks.for_waiting({}, _q("I couldn't produce a next-step decision (the orchestrator agent "
                                    "returned nothing, 3 attempts). Check the runtime, then reply to resume."))
        assert a["kind"] == "stuck" and a["attempts"] == 3
        assert a["headline"] == "The orchestrator returned nothing"

    def test_it_names_the_last_worker_error(self):
        thread = [{"kind": "worker_finished", "author": "system",
                   "text": "engineer ended without reporting a result (status=timeout): Agent exceeded 300s"}]
        thread += _q("The last few worker runs failed without reporting a result (agent_no_result) — "
                     "the runner or its config may be broken. Please check before I try again.")
        a = asks.for_waiting({}, thread)
        assert a["kind"] == "stuck" and a["error"] == "timeout — Agent exceeded 300s"

    def test_a_spending_cap_is_its_own_kind(self):
        a = asks.for_waiting({}, _q("Task spent $2.10 of its $2.00 cap. Reply to continue, or leave it parked."))
        assert a["kind"] == "budget" and a["headline"] == "Task spent $2.10 of its $2.00 cap."


class TestAQuestionOffersItsOwnOptions:
    def test_numbered_choices_become_options(self):
        a = asks.for_waiting({}, _q("What do you want me to review? Please pick one:\n"
                                    "1. The latest commits\n2) Open merge requests\n3. The whole repo"))
        assert a["kind"] == "question"
        assert a["prompt"] == "What do you want me to review?"
        assert a["options"] == [("1", "The latest commits"), ("2", "Open merge requests"), ("3", "The whole repo")]

    def test_one_numbered_line_is_not_a_menu(self):
        a = asks.for_waiting({}, _q("Which database?\n1. I think Postgres."))
        assert a["options"] == [] and a["prompt"].startswith("Which database?")

    def test_a_waiting_task_with_no_question_still_asks_for_a_reply(self):
        assert asks.for_waiting({}, [])["prompt"] == "Waiting for your reply."


class TestAMergeCardStatesWhatItRestsOn:
    def test_ready(self):
        m = asks.for_merge({"id": "mr-1", "review_status": "approved", "check_status": "passed",
                            "check_output": "1302 passed in 70s", "source_branch": "t", "target_branch": "main"},
                           "ready", "diff --git a/x b/x\n--- a/x\n+++ b/x\n+new\n-old\n+more\n")
        assert m["ready"] and ("good", "checks: 1302 passed") in m["facts"]
        assert m["diff"] == {"files": 1, "adds": 2, "dels": 1}

    def test_a_failed_check_names_the_test_and_offers_a_fix(self):
        m = asks.for_merge({"id": "mr-1", "review_status": "approved", "check_status": "failed",
                            "check_output": "FAILED tests/test_x.py::test_y - assert\n1 failed"}, "check-failed")
        assert not m["ready"] and m["broke"] == "tests/test_x.py::test_y"
        assert m["fix"].startswith("The check failed")
