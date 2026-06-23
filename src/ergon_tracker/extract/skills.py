"""Deterministic skill extraction — a curated gazetteer for résumé↔JD fit analysis (apply-assist).

Deterministic-first (like the geo/level extractors): a curated set of high-signal skills with their
surface forms/aliases, matched on word boundaries that tolerate ``c++``/``c#``/``.net``/``ci/cd``.
Precision over recall — single-letter/ambiguous tokens (bare "r", "go", "c") are deliberately omitted
to avoid false positives; canonical forms (golang, c++) cover them. Extend ``_SKILLS`` freely.
"""

from __future__ import annotations

import re

__all__ = ["extract_skills", "SKILLS"]

# canonical skill -> surface forms (lowercase) that mean it. First form is the canonical label.
_SKILLS: dict[str, tuple[str, ...]] = {
    # languages
    "python": ("python",),
    "javascript": ("javascript", "js"),
    "typescript": ("typescript", "ts"),
    "java": ("java",),
    "c++": ("c++",),
    "c#": ("c#", "csharp"),
    ".net": (".net", "dotnet"),
    "golang": ("golang", "go-lang"),
    "rust": ("rust",),
    "ruby": ("ruby",),
    "php": ("php",),
    "swift": ("swift",),
    "kotlin": ("kotlin",),
    "scala": ("scala",),
    "perl": ("perl",),
    "sql": ("sql",),
    "bash": ("bash", "shell scripting"),
    "matlab": ("matlab",),
    # web / frontend
    "react": ("react", "react.js", "reactjs"),
    "vue": ("vue", "vue.js"),
    "angular": ("angular",),
    "node.js": ("node.js", "nodejs", "node js"),
    "next.js": ("next.js", "nextjs"),
    "django": ("django",),
    "flask": ("flask",),
    "fastapi": ("fastapi",),
    "spring": ("spring boot", "spring"),
    "rails": ("ruby on rails", "rails"),
    "graphql": ("graphql",),
    "rest": ("rest api", "restful", "rest"),
    "html": ("html",),
    "css": ("css",),
    "tailwind": ("tailwind",),
    # cloud / devops
    "aws": ("aws", "amazon web services"),
    "gcp": ("gcp", "google cloud"),
    "azure": ("azure",),
    "kubernetes": ("kubernetes", "k8s"),
    "docker": ("docker",),
    "terraform": ("terraform",),
    "ci/cd": ("ci/cd", "cicd", "continuous integration", "continuous delivery"),
    "jenkins": ("jenkins",),
    "ansible": ("ansible",),
    "linux": ("linux",),
    "git": ("git",),
    "kafka": ("kafka",),
    "rabbitmq": ("rabbitmq",),
    "microservices": ("microservices", "microservice"),
    # data / ml
    "machine learning": ("machine learning", "ml"),
    "deep learning": ("deep learning",),
    "nlp": ("nlp", "natural language processing"),
    "computer vision": ("computer vision",),
    "pytorch": ("pytorch",),
    "tensorflow": ("tensorflow",),
    "scikit-learn": ("scikit-learn", "sklearn"),
    "pandas": ("pandas",),
    "numpy": ("numpy",),
    "spark": ("apache spark", "pyspark", "spark"),
    "hadoop": ("hadoop",),
    "airflow": ("airflow",),
    "dbt": ("dbt",),
    "tableau": ("tableau",),
    "power bi": ("power bi", "powerbi"),
    "looker": ("looker",),
    "snowflake": ("snowflake",),
    "data engineering": ("data engineering",),
    "etl": ("etl", "elt"),
    "llm": ("llm", "large language model"),
    # databases
    "postgresql": ("postgresql", "postgres"),
    "mysql": ("mysql",),
    "mongodb": ("mongodb", "mongo"),
    "redis": ("redis",),
    "elasticsearch": ("elasticsearch",),
    "dynamodb": ("dynamodb",),
    "cassandra": ("cassandra",),
    "bigquery": ("bigquery",),
    # methodology / general professional
    "agile": ("agile",),
    "scrum": ("scrum",),
    "kanban": ("kanban",),
    "jira": ("jira",),
    "tdd": ("tdd", "test-driven development"),
    "unit testing": ("unit testing",),
    "project management": ("project management",),
    "stakeholder management": ("stakeholder management",),
    "data analysis": ("data analysis", "data analytics"),
    "excel": ("excel",),
    "salesforce": ("salesforce",),
    "sap": ("sap",),
    "figma": ("figma",),
    "seo": ("seo",),
    "accounting": ("accounting",),
    "financial modeling": ("financial modeling", "financial modelling"),
}

SKILLS = tuple(_SKILLS)

# Per-form matcher. Boundaries exclude letters/digits/+/# so "ml" never matches inside "html" and
# "c++" needs its own ++. We deliberately DON'T treat . or / as token chars in the boundary, so a
# trailing sentence period ("AWS." / "Docker.") still bounds the word — the literal forms ("node.js",
# "ci/cd", ".net") carry their own . and / and match verbatim. Compiled once.
_MATCHERS: list[tuple[str, re.Pattern[str]]] = [
    (canon, re.compile(rf"(?<![a-z0-9+#]){re.escape(form)}(?![a-z0-9+#])"))
    for canon, forms in _SKILLS.items()
    for form in forms
]


def extract_skills(text: str | None) -> set[str]:
    """Return the canonical skills mentioned in ``text`` (deterministic gazetteer match)."""
    if not text:
        return set()
    t = text.lower()
    return {canon for canon, rx in _MATCHERS if rx.search(t)}
