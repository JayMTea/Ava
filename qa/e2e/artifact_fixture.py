"""Seed recorded artifacts into the E2E runner's isolated AVA_HOME."""
import copy
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def seed() -> dict:
    from ava_bridge import chat_store, connectors, data_artifacts, settings

    target = Path(settings.AVA_HOME) / "connectors" / "qa-analytics"
    target.mkdir(parents=True, exist_ok=True)
    (target / "connector.yaml").write_text(
        "label: Analytics\nenabled: true\nui:\n  embed: iframe\n"
        "  upstream: http://127.0.0.1:9\n  color: '#327ea8'\n", encoding="utf-8")
    connectors.load(force=True)
    ident = str(uuid.uuid4())
    artifact = {
        "schema_version": "ava-artifact/1", "type": "analytics", "result_id": ident,
        "mode": "snapshot", "title": "ACS 2024 / 1-year — people by state", "chart_type": "bar",
        "result": {"schema_version": "analysis-result/1", "id": ident, "mode": "snapshot",
                   "title": "ACS 2024 / 1-year — people by state", "created_at": "2026-01-01T00:00:00Z",
                   "unit": "people", "metric_id": "census.weighted_count", "metric_version": "qa",
                   "method": "Weighted count", "limitations": [], "row_count": 2,
                   "filters": {"release": "ACS 2024 / 1-year", "states": ["06", "41"],
                               "record_type": "person", "geography_level": "state"},
                   "rows": [{"state": "06", "puma": "", "label": "California", "value": 120,
                             "moe_90": None, "sample_records": 4},
                            {"state": "41", "puma": "", "label": "Oregon", "value": 30,
                             "moe_90": None, "sample_records": 2}],
                   "columns": [{"name": "label"}, {"name": "value"}],
                   "sources": [], "citations": [{"title": "Census PUMS documentation",
                                                  "url": "https://www.census.gov/programs-surveys/acs/microdata.html"}]},
    }
    # Optional owner-local recorded public result, never required by the suite.
    fixture = os.environ.get("QA_ANALYTICS_FIXTURE")
    if fixture:
        artifact = json.loads(Path(fixture).read_text(encoding="utf-8-sig"))
    legacy = data_artifacts.capture("qa-analytics", {"_meta": {"ava/artifact": artifact}})["structuredContent"]["artifact"]
    image = copy.deepcopy(artifact)
    image["visualization"] = {"format": "svg", "result_id": artifact["result_id"],
                              "content": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 220"><title>Recorded chart fixture</title><rect x="20" y="20" width="660" height="30" fill="#327ea8"/></svg>'}
    recorded = data_artifacts.capture("qa-analytics", {"_meta": {"ava/artifact": image}})["structuredContent"]["artifact"]
    first = chat_store.chat_new("California and Oregon")
    chat_store.chat_append(first["id"], "user", "Use Analytics to compare the estimated number of people in California and Oregon from ACS 2024, 1-year, by state. Find the relevant dataset, use the approved weighted-count analysis, and show the recorded chart with its source citations. Keep the answer brief.")
    chat_store.chat_append(first["id"], "assistant", "California has the larger estimated population.", artifact=legacy)
    second = chat_store.chat_new("Another conversation")
    chat_store.chat_append(second["id"], "user", "A separate conversation.")
    third = chat_store.chat_new("Chart supplied by Analytics")
    chat_store.chat_append(third["id"], "assistant", "Your recorded chart.", artifact=recorded)
    generic_id = str(uuid.uuid4())
    generic = {"schema_version": "ava-artifact/3", "id": generic_id,
               "type": "analytics", "mode": "snapshot", "title": "Revenue by department",
               "chart_type": "bar", "result": {
                   "schema_version": "analysis-result/2", "id": generic_id, "mode": "snapshot",
                   "title": "Revenue by department", "created_at": "2026-09-13T12:00:00Z",
                   "unit": "USD", "dimension_label": "Department", "method": "Net revenue",
                   "columns": [{"name": "label"}, {"name": "value"}], "row_count": 2,
                   "rows": [{"label": "Returns", "value": -12.5}, {"label": "Sales", "value": 25.75}],
                   "sources": [], "citations": [], "limitations": []}}
    portable = data_artifacts.capture("qa-analytics", {"_meta": {"ava/artifact": generic}})["structuredContent"]["artifact"]
    fourth = chat_store.chat_new("Application-neutral result")
    chat_store.chat_append(fourth["id"], "assistant", "Your department revenue.", artifact=portable)
    return {"QA_CHART_CHAT": first["id"], "QA_OTHER_CHAT": second["id"],
            "QA_IMAGE_CHAT": third["id"], "QA_CHART_ID": legacy["id"],
            "QA_IMAGE_ID": recorded["id"], "QA_GENERIC_CHAT": fourth["id"]}


if __name__ == "__main__":
    print(json.dumps(seed()))
