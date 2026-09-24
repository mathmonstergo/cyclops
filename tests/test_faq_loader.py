import json

import pytest

from cyclops.faq_loader import import_faqs, load_faq_rows, validate_faq_row


def test_load_faq_rows_reads_jsonl(tmp_path):
    path = tmp_path / "faq.jsonl"
    row = {
        "id": "doc_0001",
        "doc_type": "faq_qa",
        "question": "问题",
        "answer": "答案",
        "confidence": "high",
        "status": "usable",
        "embedding_text": "问题：问题\n答案：答案",
    }
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    assert load_faq_rows(path)[0]["id"] == "doc_0001"


def test_validate_faq_row_reports_missing_field():
    row = {"id": "x"}
    errors = validate_faq_row(row)
    assert "question" in errors
    assert "embedding_text" in errors


def test_load_faq_rows_reports_invalid_json_with_path_and_line(tmp_path):
    path = tmp_path / "faq.jsonl"
    valid_row = {
        "id": "doc_0001",
        "question": "问题",
        "answer": "答案",
        "confidence": "high",
        "status": "usable",
        "embedding_text": "问题：问题\n答案：答案",
    }
    path.write_text(
        json.dumps(valid_row, ensure_ascii=False) + "\n" + "{bad json\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_faq_rows(path)

    assert f"{path}:2" in str(exc_info.value)


def test_load_faq_rows_rejects_non_object_json_with_path_and_line(tmp_path):
    path = tmp_path / "faq.jsonl"
    path.write_text("123\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_faq_rows(path)

    message = str(exc_info.value)
    assert f"{path}:1" in message
    assert "expected JSON object" in message


def test_import_faqs_embeds_upserts_and_returns_count(tmp_path):
    """FAQ 导入应逐条生成向量、写库并返回计数。"""
    path = tmp_path / "faq.jsonl"
    rows = [
        {
            "id": "doc_0001",
            "question": "问题1",
            "answer": "答案1",
            "confidence": "high",
            "status": "usable",
            "embedding_text": "问题：问题1\n答案：答案1",
        },
        {
            "id": "doc_0002",
            "question": "问题2",
            "answer": "答案2",
            "confidence": "high",
            "status": "usable",
            "embedding_text": "问题：问题2\n答案：答案2",
        },
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    class FakeEmbeddings:
        model = "embedding-current"
        dimensions = 1

        def __init__(self):
            self.texts = []

        def embed(self, text):
            self.texts.append(text)
            return [float(len(self.texts))]

    class FakeDb:
        def __init__(self):
            self.upserts = []

        def upsert_faq(self, row, vector, *, embedding_model, embedding_dimensions):
            """记录 FAQ 正文、向量及模型元数据。"""
            self.upserts.append(
                (row, vector, embedding_model, embedding_dimensions)
            )

    embeddings = FakeEmbeddings()
    db = FakeDb()

    count = import_faqs(path, db, embeddings)

    assert count == 2
    assert embeddings.texts == [row["embedding_text"] for row in rows]
    assert db.upserts == [
        (rows[0], [1.0], "embedding-current", 1),
        (rows[1], [2.0], "embedding-current", 1),
    ]
