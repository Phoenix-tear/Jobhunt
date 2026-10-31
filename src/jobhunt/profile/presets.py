"""Standard role family presets for Computer Science domains (Brief v12)."""

from __future__ import annotations

from typing import Any

ROLE_FAMILY_PRESETS: dict[str, dict[str, Any]] = {
    "genai": {
        "id": "genai",
        "label": "Generative AI & LLM Agents",
        "keywords": [
            "rag", "agent", "agents", "langchain", "llamaindex", "autogen", "crewai",
            "vector database", "qdrant", "chroma", "pinecone", "pgvector", "embeddings",
            "llm", "llms", "large language models", "prompt engineering", "fine-tuning",
            "lora", "vllm", "ollama", "guardrails", "evaluation", "hallucination",
        ],
        "title_patterns": [
            "ai agent", "generative ai", "genai", "rag", "llm engineer", "ai engineer",
            "applied ai", "ai solutions",
        ],
        "recipe": "ai_agents_rag",
        "weight": 1.0,
        "score_adjustment": 0.0,
    },
    "ai_ml": {
        "id": "ai_ml",
        "label": "AI & Machine Learning",
        "keywords": [
            "machine learning", "deep learning", "neural network", "pytorch", "tensorflow",
            "scikit-learn", "xgboost", "computer vision", "nlp", "model training",
            "feature engineering", "cross-validation", "hyperparameter", "supervised learning",
        ],
        "title_patterns": [
            "machine learning engineer", "ml engineer", "ai developer", "applied scientist",
            "ml intern", "computer vision engineer", "nlp engineer",
        ],
        "recipe": "ml_data",
        "weight": 1.0,
        "score_adjustment": 0.0,
    },
    "fullstack": {
        "id": "fullstack",
        "label": "Full Stack Software Engineering",
        "keywords": [
            "react", "next.js", "typescript", "javascript", "node.js", "tailwindcss",
            "fastapi", "rest api", "postgresql", "mongodb", "html", "css", "state management",
        ],
        "title_patterns": [
            "full stack engineer", "full stack developer", "fullstack", "software developer",
            "web developer", "frontend engineer",
        ],
        "recipe": "fullstack",
        "weight": 1.0,
        "score_adjustment": 0.0,
    },
    "backend": {
        "id": "backend",
        "label": "Backend Software Engineering",
        "keywords": [
            "backend", "fastapi", "python", "go", "golang", "java", "spring boot",
            "microservices", "postgresql", "redis", "restful api", "grpc", "docker",
            "database indexing", "caching", "concurrency",
        ],
        "title_patterns": [
            "backend engineer", "backend developer", "software engineer", "systems engineer",
            "api developer", "server engineer",
        ],
        "recipe": "backend_swe",
        "weight": 1.0,
        "score_adjustment": 0.0,
    },
    "data": {
        "id": "data",
        "label": "Data Engineering & Analytics",
        "keywords": [
            "data pipeline", "etl", "sql", "airflow", "spark", "pyspark", "snowflake",
            "databricks", "dbt", "kafka", "data warehouse", "pandas", "data modeling",
        ],
        "title_patterns": [
            "data engineer", "data platform engineer", "analytics engineer", "big data engineer",
        ],
        "recipe": "ml_data",
        "weight": 0.9,
        "score_adjustment": -0.05,
    },
    "devops_cloud": {
        "id": "devops_cloud",
        "label": "DevOps & Cloud Infrastructure",
        "keywords": [
            "docker", "kubernetes", "k8s", "aws", "gcp", "azure", "terraform",
            "ci/cd", "github actions", "linux", "bash", "prometheus", "grafana", "helm",
        ],
        "title_patterns": [
            "devops engineer", "cloud engineer", "platform engineer", "sre",
            "site reliability engineer", "infrastructure engineer",
        ],
        "recipe": "backend_swe",
        "weight": 0.85,
        "score_adjustment": -0.10,
    },
    "mobile": {
        "id": "mobile",
        "label": "Mobile Application Development",
        "keywords": [
            "react native", "flutter", "ios", "android", "swift", "kotlin", "dart",
            "mobile development", "app store", "play store",
        ],
        "title_patterns": [
            "mobile engineer", "mobile developer", "ios developer", "android developer",
            "flutter developer", "react native developer",
        ],
        "recipe": "fullstack",
        "weight": 0.8,
        "score_adjustment": -0.10,
    },
    "security": {
        "id": "security",
        "label": "Cybersecurity & Application Security",
        "keywords": [
            "appsec", "application security", "owasp", "penetration testing", "vulnerability",
            "cryptography", "identity", "oauth", "jwt", "zero trust",
        ],
        "title_patterns": [
            "security engineer", "application security engineer", "infosec engineer",
            "cybersecurity analyst",
        ],
        "recipe": "backend_swe",
        "weight": 0.8,
        "score_adjustment": -0.10,
    },
    "qa": {
        "id": "qa",
        "label": "QA & Test Automation",
        "keywords": [
            "pytest", "selenium", "playwright", "cypress", "test automation",
            "integration testing", "e2e testing", "unit testing", "load testing",
        ],
        "title_patterns": [
            "qa engineer", "quality assurance", "test automation engineer", "sdet",
        ],
        "recipe": "backend_swe",
        "weight": 0.75,
        "score_adjustment": -0.15,
    },
    "salesforce": {
        "id": "salesforce",
        "label": "Salesforce & CRM Engineering",
        "keywords": [
            "salesforce", "apex", "soql", "agentforce", "lightning web components",
            "lwc", "visualforce", "flow", "salesforce platform", "crm", "integration",
        ],
        "title_patterns": [
            "salesforce developer", "salesforce engineer", "apex developer", "crm developer",
            "salesforce administrator",
        ],
        "recipe": "salesforce",
        "weight": 0.75,
        "score_adjustment": -0.15,
    },
}
