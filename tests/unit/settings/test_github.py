"""What Settings › Software asks GitHub (croom.settings.github, spec 2026-10-29 section 6), on a fake transport: the newest commit of main,
the titles between the running commit and it, a 10-minute cache, and "Can't check for updates right now" when GitHub does not answer."""

import json

import pytest

from croom.settings.github import API, CACHE_S, CANT_CHECK, FAILURE_CACHE_S, MAX_CHANGES, GitHub, GitHubError, fetch_url

A = "a" * 40
B = "b" * 40
REPO = f"{API}/repos/ben-abeo/croom.to"


def commit(sha, message, date="2026-10-30T14:02:11Z"):
    return {"sha": sha, "commit": {"message": message, "committer": {"date": date}, "author": {"date": "2020-01-01T00:00:00Z"}}}


def hexes(n):
    return f"{n:040x}"


class Transport:
    """GitHub's API as a dict of url -> (status, JSON); anything else is a 404. `down` makes every request fail as a network would;
    `asked` lists the urls."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.asked = []
        self.down = False

    def __call__(self, url):
        self.asked.append(url)
        if self.down:
            raise OSError("Network is unreachable")
        status, body = self.answers.get(url, (404, {"message": "Not Found"}))
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def github_on(answers=None):
    transport, clock = Transport(answers), Clock()
    return GitHub(fetch=transport, clock=clock), transport, clock


def compare(base, head, commits, status="ahead", total=None):
    return {"status": status, "ahead_by": len(commits), "behind_by": 0, "total_commits": len(commits) if total is None else total,
            "base_commit": commit(base, "The running one\n\nWith a body", "2026-10-28T09:00:00Z"), "commits": commits}


NEWEST = {f"{REPO}/commits/main": (200, commit(B, "feat(settings): the Software page\n\nA body that is not shown"))}


def test_the_newest_commit_of_main():
    github, transport, _ = github_on(NEWEST)
    assert github.newest() == {"commit": B, "short": "bbbbbbb", "title": "feat(settings): the Software page", "date": "2026-10-30T14:02:11Z"}
    assert transport.asked == [f"{REPO}/commits/main"]


def test_the_summary_lists_the_titles_between_newest_first():
    answers = dict(NEWEST)
    answers[f"{REPO}/compare/{A}...{B}"] = (200, compare(A, B, [commit(hexes(1), "fix: one"), commit(hexes(2), "feat: two\n\nbody"),
                                                              commit(B, "feat(settings): the Software page")]))
    github, _, _ = github_on(answers)
    summary = github.summary(A)
    assert summary["newest"]["commit"] == B and summary["up_to_date"] is False and summary["error"] is None
    assert [c["title"] for c in summary["changes"]] == ["feat(settings): the Software page", "feat: two", "fix: one"]
    assert summary["running"] == {"commit": A, "short": "aaaaaaa", "title": "The running one", "date": "2026-10-28T09:00:00Z"}
    assert summary["more"] == 0


def test_a_long_list_is_cut_and_says_how_many_more():
    answers = dict(NEWEST)
    commits = [commit(hexes(n + 1), f"change {n}") for n in range(250)]
    answers[f"{REPO}/compare/{A}...{B}"] = (200, compare(A, B, commits, total=300))
    summary = github_on(answers)[0].summary(A)
    assert len(summary["changes"]) == MAX_CHANGES and summary["changes"][0]["title"] == "change 249"
    assert summary["more"] == 300 - MAX_CHANGES


def test_up_to_date():
    answers = dict(NEWEST)
    answers[f"{REPO}/compare/{B}...{B}"] = (200, compare(B, B, [], status="identical"))
    summary = github_on(answers)[0].summary(B)
    assert summary["up_to_date"] is True and summary["changes"] == [] and summary["error"] is None


def test_a_version_github_does_not_know_has_no_list_but_the_newest_is_shown():
    summary = github_on(NEWEST)[0].summary(A)          # the compare is a 404
    assert summary["newest"]["commit"] == B and summary["changes"] is None and summary["error"] is None


def test_an_unknown_running_version_shows_the_newest_only():
    github, transport, _ = github_on(NEWEST)
    summary = github.summary("unknown")
    assert summary["newest"]["commit"] == B and summary["up_to_date"] is None and summary["changes"] is None
    assert transport.asked == [f"{REPO}/commits/main"]


@pytest.mark.parametrize("answer", [(500, {"message": "boom"}), (403, {"message": "API rate limit exceeded"}), (200, b"<html>"),
                                    (200, {"sha": "not a commit"}), (200, [1, 2])])
def test_an_answer_that_is_not_what_it_should_be_is_cant_check(answer):
    summary = github_on({f"{REPO}/commits/main": answer})[0].summary(A)
    assert summary == {"newest": None, "running": None, "changes": None, "more": 0, "up_to_date": None, "error": CANT_CHECK}


def test_no_network_is_cant_check():
    github, transport, _ = github_on(NEWEST)
    transport.down = True
    assert github.summary(A)["error"] == "Can't check for updates right now"
    with pytest.raises(GitHubError, match=CANT_CHECK):
        github.newest()


def test_answers_are_kept_for_10_minutes():
    github, transport, clock = github_on(NEWEST)
    github.newest()
    clock.now += CACHE_S - 1
    github.newest()
    assert len(transport.asked) == 1
    clock.now += 2
    github.newest()
    assert len(transport.asked) == 2
    assert CACHE_S == 600


def test_a_failure_is_kept_for_a_minute_so_a_page_that_polls_does_not_use_up_the_hour():
    github, transport, clock = github_on(NEWEST)
    transport.down = True
    for _ in range(5):
        assert github.summary(A)["error"] == CANT_CHECK
    assert len(transport.asked) == 1
    transport.down = False
    clock.now += FAILURE_CACHE_S + 1
    assert github.summary(A)["newest"]["commit"] == B


@pytest.mark.parametrize("status, on", [("ahead", True), ("identical", True), ("behind", False), ("diverged", False)])
def test_on_branch_is_a_commit_in_mains_history(status, on):
    github, transport, _ = github_on({f"{REPO}/compare/{A}...main": (200, compare(A, B, [], status=status))})
    assert github.on_branch(A) is on
    assert transport.asked == [f"{REPO}/compare/{A}...main"]


def test_on_branch_refuses_what_is_not_a_commit_without_asking():
    github, transport, _ = github_on()
    assert github.on_branch("main") is False and github.on_branch("../../x") is False
    assert transport.asked == []
    assert github.on_branch(A) is False                    # a 404: GitHub knows no such commit
    github, transport, _ = github_on()
    transport.down = True
    with pytest.raises(GitHubError):
        github.on_branch(B)


def test_the_real_transport_sends_githubs_headers(monkeypatch):
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def urlopen(request, timeout):
        seen["url"], seen["headers"], seen["timeout"] = request.full_url, dict(request.header_items()), timeout
        return Response()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert fetch_url(f"{REPO}/commits/main") == (200, b"{}")
    assert seen["headers"]["Accept"] == "application/vnd.github+json" and seen["timeout"] == 10
