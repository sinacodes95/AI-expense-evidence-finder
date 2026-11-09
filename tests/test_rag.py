import json

import httpx
from fastapi.testclient import TestClient
from openai import OpenAI, AuthenticationError

from app.config import Settings
from app.main import create_app
from app.provider import AnswerDraft, Claim, EvidenceReference, OpenAIProvider
from app.rag import retrieve, validate_answer
from tests.conftest import create_client, upload

INSURANCE = "Period of insurance: 1 July 2026 to 30 June 2027."


class FakeProvider:
    def __init__(self):
        self.embedded = []
        self.seen_sources = []

    def embed(self, texts):
        self.embedded.extend(texts)
        return [[1.0, 0.0] if "insurance" in text.lower() or "risk protection" in text.lower() else [0.0, 1.0] for text in texts]

    def answer(self, question, chunks):
        self.seen_sources = chunks
        row = next(row for row in chunks if INSURANCE in row["text"])
        return AnswerDraft(status="answered", claims=[Claim(text="The insurance covers 1 July 2026 to 30 June 2027.", evidence=[EvidenceReference(chunk_id=row["id"], quote=INSURANCE)])])


def test_rag_citations_cache_and_client_filter(tmp_path):
    provider = FakeProvider()
    with TestClient(create_app(Settings(data_dir=tmp_path), provider)) as api:
        client = create_client(api)
        other = create_client(api, "Other client")
        upload(api, client, INSURANCE)
        upload(api, other, "Other client SECRET insurance 99999.")
        for _ in range(2):
            response = api.post(f"/api/clients/{client}/ask", json={"question": "When does the risk protection end?"})
            assert response.status_code == 200
            answer = response.json()
            assert answer["status"] == "answered"
            assert answer["claims"][0]["citations"] == [1]
            assert answer["sources"][0]["quote"] == INSURANCE
        assert provider.embedded.count(INSURANCE) == 1
        assert all("SECRET" not in text for text in provider.embedded)
        assert all("SECRET" not in row["text"] for row in provider.seen_sources)


def test_semantic_retrieval_and_model_cache_invalidation(tmp_path):
    provider = FakeProvider()
    with TestClient(create_app(Settings(data_dir=tmp_path), provider)) as api:
        client = create_client(api)
        upload(api, client, INSURANCE)
        upload(api, client, "Office furniture invoice for a desk.", "desk.txt")
        db = api.app.state.database
        result = retrieve(db, client, "risk protection", provider, "model-a", limit=1)
        assert result[0]["text"] == INSURANCE
        retrieve(db, client, "risk protection", provider, "model-b", limit=1)
        assert provider.embedded.count(INSURANCE) == 2


def test_citation_validation_rejects_invented_ids_quotes_and_uncited_claims():
    chunks = [{"id": "known", "document_id": "doc", "filename": "invoice.txt", "page": 2, "text": INSURANCE}]
    invalid = [
        Claim(text="The period is annual.", evidence=[]),
        Claim(text="The period is annual.", evidence=[EvidenceReference(chunk_id="invented", quote=INSURANCE)]),
        Claim(text="Payment was received.", evidence=[EvidenceReference(chunk_id="known", quote="Paid in full on 1 July 2026.")]),
    ]
    for claim in invalid:
        answer = validate_answer(AnswerDraft(status="answered", claims=[claim]), chunks)
        assert answer["status"] == "insufficient_evidence"
        assert answer["claims"] == []
        assert answer["sources"] == []


def test_conflicting_sources_are_preserved_and_whitespace_is_normalized():
    chunks = [{"id": "old", "document_id": "a", "filename": "old.txt", "page": 1, "text": "Payment terms:\n30 days."},
              {"id": "new", "document_id": "b", "filename": "new.txt", "page": 3, "text": "Payment terms: 14 days."}]
    draft = AnswerDraft(status="conflicting_evidence", claims=[
        Claim(text="One document states 30 days.", evidence=[EvidenceReference(chunk_id="old", quote="Payment terms: 30 days.")]),
        Claim(text="The other states 14 days.", evidence=[EvidenceReference(chunk_id="new", quote="Payment terms: 14 days.")])])
    answer = validate_answer(draft, chunks)
    assert answer["status"] == "conflicting_evidence"
    assert [source["page"] for source in answer["sources"]] == [1, 3]


def test_insufficient_evidence_does_not_display_generated_claims():
    draft = AnswerDraft(status="insufficient_evidence", claims=[Claim(text="Unverified content", evidence=[])])
    assert validate_answer(draft, [])["claims"] == []


def test_provider_errors_are_actionable_and_do_not_leak_request_data(tmp_path):
    class BrokenProvider(FakeProvider):
        def embed(self, texts):
            response = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/embeddings"))
            raise AuthenticationError("SECRET-UPLOADED-TEXT", response=response, body=None)
    with TestClient(create_app(Settings(data_dir=tmp_path), BrokenProvider())) as api:
        client = create_client(api)
        upload(api, client, INSURANCE)
        response = api.post(f"/api/clients/{client}/ask", json={"question": "Insurance?"})
        assert response.status_code == 503
        assert "SECRET" not in response.text
        assert "API key" in response.text


def test_actual_openai_sdk_request_and_structured_response_contract(tmp_path):
    seen = []

    def transport(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"object": "list", "model": "text-embedding-3-small", "data": [
                {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}], "usage": {"prompt_tokens": 3, "total_tokens": 3}})
        draft = {"status": "answered", "claims": [{"text": "The coverage ends 30 June 2027.", "evidence": [{"chunk_id": "c1", "quote": INSURANCE}]}]}
        return httpx.Response(200, json={"id": "resp_test", "object": "response", "created_at": 1, "status": "completed", "model": "gpt-4.1-mini",
            "output": [{"id": "msg_test", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(draft), "annotations": []}]}]})

    settings = Settings(data_dir=tmp_path, api_key="test-key")
    provider = OpenAIProvider(settings)
    provider.client.close()
    with httpx.Client(transport=httpx.MockTransport(transport)) as http_client:
        provider.client = OpenAI(api_key="test-key", http_client=http_client)
        assert provider.embed([INSURANCE]) == [[1.0, 0.0]]
        answer = provider.answer("When does coverage end?", [{"id": "c1", "filename": "insurance.txt", "page": 1, "text": INSURANCE}])
        assert answer.status == "answered"
        assert answer.claims[0].evidence[0].chunk_id == "c1"
    assert seen[1][1]["store"] is False
    assert seen[1][1]["text"]["format"]["type"] == "json_schema"
    assert "untrusted" in seen[1][1]["instructions"]
