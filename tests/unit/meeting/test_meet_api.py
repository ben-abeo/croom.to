"""
The participants list from Google's Meet REST API (Meet controls plan Phase B, Task 6): a 403 falls back after one log line and is not retried
within 5 minutes; an organizer outside the domain makes no request; the token's subject is the organizer. A local aiohttp server stands in for
meet.googleapis.com and the credentials are fake (but for the subject test, which uses a throwaway key and never signs in), so nothing reaches
Google.
"""

import base64
import json
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from croom.meeting import meet_api
from croom.meeting.meet_api import MeetParticipants, SCOPE

ORGANIZER = "ann@crystalpm.com"
CODE = "abc-mnop-xyz"


class FakeCredentials:
    valid = False
    token = None

    def __init__(self, organizer):
        self.organizer = organizer

    def refresh(self, request):
        self.token = "token-for-" + self.organizer
        self.valid = True


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class FakeMeet:
    """meet.googleapis.com's v2 as far as the room asks: a space, its live conference, its participants in two pages."""

    def __init__(self, status=200):
        self.status = status
        self.requests = []
        self.app = web.Application()
        self.app.router.add_get("/v2/spaces/{code}", self.space)
        self.app.router.add_get("/v2/conferenceRecords", self.conferences)
        self.app.router.add_get("/v2/conferenceRecords/{id}/participants", self.participants)

    def seen(self, request):
        self.requests.append((request.path, dict(request.query), request.headers.get("Authorization")))
        return None if self.status == 200 else web.json_response({"error": {"message": "no grant"}}, status=self.status)

    async def space(self, request):
        return self.seen(request) or web.json_response({"name": "spaces/s1", "meetingCode": request.match_info["code"]})

    async def conferences(self, request):
        return self.seen(request) or web.json_response({"conferenceRecords": [{"name": "conferenceRecords/c1"}]})

    async def participants(self, request):
        refused = self.seen(request)
        if refused is not None:
            return refused
        if not request.query.get("pageToken"):
            return web.json_response({"participants": [{"signedinUser": {"user": "users/1", "displayName": "Ann Lee"}},
                                                       {"anonymousUser": {"displayName": "Guest 7"}}], "nextPageToken": "more"})
        return web.json_response({"participants": [{"phoneUser": {"displayName": "+1 555 0100"}}, {"signedinUser": {"user": "users/2"}}]})


async def api_against(fake, **options):
    server = TestServer(fake.app)
    await server.start_server()
    made = []

    def credentials_for(path, organizer):
        made.append((path, organizer))
        return FakeCredentials(organizer)

    clock = options.pop("clock", Clock())
    api = MeetParticipants("crystalpm.com", "/etc/croom/google.json", base_url=str(server.make_url("")).rstrip("/"),
                           credentials_for=credentials_for, clock=clock, **options)
    return api, server, made


async def test_the_names_of_whoever_is_in_the_call_with_both_pages_followed():
    fake = FakeMeet()
    api, server, made = await api_against(fake)
    try:
        assert await api.names(ORGANIZER, CODE) == ["Ann Lee", "Guest 7", "+1 555 0100"]
    finally:
        await api.close()
        await server.close()
    paths = [(path, query.get("filter")) for path, query, _ in fake.requests]
    assert paths[0][0] == "/v2/spaces/" + CODE
    assert paths[1] == ("/v2/conferenceRecords", 'space.name="spaces/s1" AND end_time IS NULL')
    assert paths[2] == ("/v2/conferenceRecords/c1/participants", "latest_end_time IS NULL")
    assert all(auth == "Bearer token-for-" + ORGANIZER for _, _, auth in fake.requests)
    assert made == [("/etc/croom/google.json", ORGANIZER)]


async def test_a_403_falls_back_after_one_log_line_and_is_not_retried_within_five_minutes(caplog):
    fake, clock = FakeMeet(status=403), Clock()
    api, server, _ = await api_against(fake, clock=clock)
    try:
        with caplog.at_level(logging.DEBUG, logger=meet_api.logger.name):
            assert await api.names(ORGANIZER, CODE) is None
            assert len(fake.requests) == 1
            clock.now += 299
            assert await api.names(ORGANIZER, CODE) is None
            assert len(fake.requests) == 1                       # still refused: nothing asked
            clock.now += 2
            assert await api.names(ORGANIZER, CODE) is None
            assert len(fake.requests) == 2                       # five minutes on: asked again, refused again
    finally:
        await api.close()
        await server.close()
    said = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert len(said) == 1 and "403" in said[0].getMessage()


async def test_an_organizer_outside_the_domain_makes_no_request():
    fake = FakeMeet()
    api, server, made = await api_against(fake)
    try:
        for organizer in ("bob@elsewhere.com", "bob@crystalpm.com.evil.example", "", "crystalpm.com", "x@sub.crystalpm.com"):
            assert await api.names(organizer, CODE) is None
        assert await api.names(ORGANIZER, "not a code") is None
    finally:
        await api.close()
        await server.close()
    assert fake.requests == [] and made == []


def test_the_token_is_asked_for_in_the_organizers_name(tmp_path):
    """The default credentials are the room's service-account key acting as the organizer (domain-wide delegation): the signed request for the
    token names the organizer as its subject and the Meet scope. Nothing is sent: the assertion is built, not exchanged."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    path = tmp_path / "key.json"
    path.write_text(json.dumps({"type": "service_account", "project_id": "p", "private_key_id": "k", "private_key": key,
                                "client_email": "room@p.iam.gserviceaccount.com", "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token"}))
    credentials = meet_api.service_account_credentials(str(path), ORGANIZER)
    assertion = credentials._make_authorization_grant_assertion().decode()
    claims = json.loads(base64.urlsafe_b64decode(assertion.split(".")[1] + "=="))
    assert claims["sub"] == ORGANIZER
    assert claims["scope"] == SCOPE == "https://www.googleapis.com/auth/meetings.space.readonly"
