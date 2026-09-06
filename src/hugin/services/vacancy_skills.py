from __future__ import annotations

import re

_TECHNOLOGY_NAMES = """
    python sql fastapi postgresql redis sqlalchemy alembic pydantic docker git pytest
    pandas numpy asyncio websocket etl elt excel vba yt yql nirvana reactor datalens
    yandexgpt speechkit qwen deepseek ollama playwright pywebview react typescript
    javascript django flask linux windows bash powershell celery rabbitmq kafka
    rest api http html css html5 nuxt jquery bootstrap tailwind scrapy selenium
    oracle plsql spark hadoop greenplum cdc debezium plc scada hmi modbus knx dali
    profibus profinet pytorch opencv ros ros2 qt qml dsp bpmn tcp/ip osi networking
    tensorflow keras siem elasticsearch elk dwh airflow pentaho kettle arenadata adb
    vertica kubernetes go node.js microservices rag pgvector langgraph langchain
    llamaindex pydanticai crewai autogen mcp qdrant milvus weaviate pinecone faiss
    chroma chromadb agentic llm openai java spring php symfony laravel rust ruby
    rails scala abap sap c++ c# .net x++ pawn mysql sqlite mongodb nosql clickhouse
    s3 trino dagster dbt jupyter scikit-learn optuna scipy matplotlib seaborn
    xgboost catboost lightgbm transformers cuda tensorrt vllm sglang yolo lora qlora
    ansible terraform helm nginx apache haproxy prometheus grafana loki graylog
    zabbix gitlab jenkins github ci/cd openshift vmware kvm proxmox ovirt opennebula
    ceph iscsi lvm san ocfs2 vlan bgp ospf cisco juniper postfix dovecot freeipa
    systemd journald netlink buildroot yocto rtos socket.io aiohttp asyncpg
    requests httpx uvicorn gunicorn grpc protobuf htmx
    """
_NAMES = frozenset(_TECHNOLOGY_NAMES.split())
_ALIASES = {
    "postgres": "postgresql",
    "psql": "postgresql",
    "pyspark": "spark",
    "torch": "pytorch",
    "cpp": "c++",
    "qt5": "qt",
    "qt6": "qt",
    "k8s": "kubernetes",
    "golang": "go",
    "nodejs": "node.js",
    "dotnet": ".net",
    "sklearn": "scikit-learn",
    "llama-index": "llamaindex",
    "pydantic-ai": "pydanticai",
    "crew-ai": "crewai",
    "tcpip": "tcp/ip",
    "плк": "plc",
    "скада": "scada",
    "микросервисы": "microservices",
    "microservice": "microservices",
    "excel-macros": "vba",
    "data-warehouse": "dwh",
    "computer-vision": "opencv",
    "signal-processing": "dsp",
}
_COMPOUNDS = (
    (r"\bpl\s*/\s*sql\b", "plsql"),
    (r"\btcp\s*/\s*ip\b", "tcp/ip"),
    (r"\bci\s*/\s*cd\b", "ci/cd"),
    (r"\bcomputer\s+vision\b", "opencv"),
    (r"\bdata\s+warehouse\b", "dwh"),
    (r"\b(?:iec|мэк)\s*61131\b", "plc"),
    (r"(?<!\w)[c\u0441]\+\+(?:11|14|17|20|23)?(?!\w)", "c++"),
)
_TERMS = re.compile(
    r"(?<![\w+#])(?:"
    + "|".join(re.escape(term) for term in sorted(_NAMES | _ALIASES.keys(), key=len, reverse=True))
    + r")(?![\w+#])",
    re.I,
)


def skill_terms(text: str) -> set[str]:
    normalized = text.casefold()
    result: set[str] = set()
    for pattern, name in _COMPOUNDS:
        if re.search(pattern, normalized):
            result.add(name)
            normalized = re.sub(pattern, " ", normalized)
    result.update(
        _ALIASES.get(match.group(), match.group()) for match in _TERMS.finditer(normalized)
    )
    return result
