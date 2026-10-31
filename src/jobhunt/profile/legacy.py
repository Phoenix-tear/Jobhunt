"""Factory for default legacy profile (Brief v11 Phase 2).

Produces an exact typed representation of legacy parameters:
- B.Tech CSE (AI & ML), class of 2027
- Level policy: max_years=2, penalty=6, cap=12, junior_boost=6
- Bases: ai (authoritative), fullstack, backend (optional)
- 7 role families & recipes (ai_agents_rag, ml_data, backend_swe, fullstack, fintech_bank, salesforce, java_backend)
- India location policy (Bengaluru, Hyderabad, Pune, etc.)
- Scoring weights 0.55 / 0.25 / 0.20
"""

from __future__ import annotations

from jobhunt.profile.model import (
    BaseResume,
    LevelPolicy,
    LocationPolicy,
    Person,
    Profile,
    ProfilePaths,
    RoleFamily,
    ScoringPolicy,
)


def build_legacy_profile() -> Profile:
    """Return the default legacy profile."""
    person = Person(
        display_name="the candidate",
        degree="B.Tech",
        field="Computer Science and Engineering (AI & ML)",
        institution="Institute of Science and Technology",
        graduation_year=2027,
        career_stage="student",
    )

    level_policy = LevelPolicy(
        max_required_years=2,
        years_penalty_per_year=6.0,
        years_penalty_cap=12.0,
        junior_boost=6.0,
        internships_wanted=True,
        graduate_programmes_wanted=True,
        senior_words=(
            "senior", "sr", "lead", "principal", "staff", "head", "director", "vp", "chief", "manager", "architect"
        ),
        excluded_roles=(
            "java", ".net", "dotnet", "php", "c#", "c++", "embedded", "firmware", "hardware",
            "salesforce", "sap", "abap", "mainframe", "cobol", "qa", "quality assurance",
            "test engineer", "automation engineer", "manual tester", "support", "it support",
            "desktop support", "helpdesk", "technician", "network engineer", "system admin", "sysadmin",
            "sales", "marketing", "business development", "recruiter", "talent acquisition",
        ),
        exclude_levels=("senior",),
        maybe_levels=("mid",),
        exempt_levels=("intern", "new_grad"),
    )

    bases = (
        BaseResume(id="ai", label="AI / ML Resume", tex_path="data/resumes/resume_ai.tex", optional=False),
        BaseResume(id="fullstack", label="Full Stack Resume", tex_path="data/resumes/resume_fullstack.tex", optional=False),
        BaseResume(id="backend", label="Java / Backend Resume", tex_path="data/resumes/resume_backend.tex", optional=True),
    )

    role_families = (
        RoleFamily(
            id="ai_agents_rag",
            label="AI Agents & RAG",
            keywords=(
                "rag", "agent", "agents", "langchain", "llamaindex", "autogen", "crewai",
                "vector database", "qdrant", "chroma", "pinecone", "pgvector", "embeddings",
                "embedding", "semantic search", "reranking", "llm", "llms", "large language models",
                "prompt engineering", "few-shot", "fine-tuning", "lora", "qlora", "vllm", "ollama",
                "guardrails", "evaluation", "hallucination", "tool use", "function calling", "mcp"
            ),
            title_patterns=("ai agent", "generative ai", "genai", "rag", "llm engineer", "ai engineer"),
            recipe="ai_agents_rag",
            weight=1.0,
            score_adjustment=0.0,
        ),
        RoleFamily(
            id="ml_data",
            label="ML & Data Science",
            keywords=(
                "pytorch", "tensorflow", "keras", "scikit-learn", "sklearn", "xgboost",
                "lightgbm", "catboost", "pandas", "numpy", "scipy", "computer vision",
                "opencv", "nlp", "transformers", "huggingface", "bert", "data pipeline",
                "etl", "airflow", "spark", "pyspark", "feature store", "mlops", "mlflow", "wandb"
            ),
            title_patterns=("machine learning", "ml engineer", "data scientist", "deep learning", "nlp engineer", "computer vision"),
            recipe="ml_data",
            weight=1.0,
            score_adjustment=0.0,
        ),
        RoleFamily(
            id="backend_swe",
            label="Backend Software Engineering",
            keywords=(
                "python", "fastapi", "django", "flask", "rest api", "graphql", "sql",
                "postgresql", "mysql", "mongodb", "redis", "docker", "kubernetes", "aws",
                "gcp", "azure", "ci/cd", "microservices", "kafka", "rabbitmq", "celery", "system design"
            ),
            title_patterns=("backend", "software engineer", "sde", "software development engineer", "platform engineer", "systems engineer"),
            recipe="backend_swe",
            weight=1.0,
            score_adjustment=0.0,
        ),
        RoleFamily(
            id="fullstack",
            label="Full Stack Engineering",
            keywords=(
                "react", "next.js", "typescript", "javascript", "tailwind", "node.js",
                "express", "html", "css", "vue", "frontend", "full stack", "fullstack"
            ),
            title_patterns=("full stack", "fullstack", "frontend", "web developer"),
            recipe="fullstack",
            weight=1.0,
            score_adjustment=0.0,
        ),
        RoleFamily(
            id="fintech_bank",
            label="Fintech & Banking",
            keywords=(
                "fintech", "banking", "payments", "trading", "financial", "ledger",
                "pci-dss", "kyc", "fraud detection"
            ),
            title_patterns=("fintech", "banking", "quantitative"),
            recipe="fintech_bank",
            weight=1.0,
            score_adjustment=0.0,
        ),
        RoleFamily(
            id="salesforce",
            label="Salesforce / Agentforce",
            keywords=(
                "salesforce", "apex", "lightning web components", "lwc", "soql",
                "sosl", "agentforce", "einstein", "salesforce crm"
            ),
            title_patterns=("salesforce", "agentforce"),
            recipe="salesforce",
            weight=1.0,
            score_adjustment=0.0,
        ),
        RoleFamily(
            id="java_backend",
            label="Java Backend",
            keywords=(
                "java", "spring", "spring boot", "hibernate", "jpa", "junit",
                "maven", "gradle", "jvm", "microservices"
            ),
            title_patterns=("java backend", "java developer", "spring boot"),
            recipe="java_backend",
            weight=1.0,
            score_adjustment=0.0,
        ),
    )

    location_policy = LocationPolicy(
        home_country="India",
        home_cities=(
            "Bengaluru", "Bangalore", "Hyderabad", "Pune", "Chennai", "Mumbai", "Delhi", "Noida", "Gurugram"
        ),
        accepted_regions=("india", "remote_open_to_india"),
        remote_scope_rules=("worldwide", "anywhere", "apac", "asia", "india"),
        workplace_types=("remote", "hybrid", "onsite"),
        relocation=False,
        work_authorization_required=("us_work_authorization", "citizenship_required", "security_clearance"),
    )

    scoring = ScoringPolicy(
        weight_fit=0.55,
        weight_similarity=0.25,
        weight_location=0.20,
        caps={},
        eligibility_blockers=("phd", "masters", "security_clearance", "us_work_authorization", "citizenship_required"),
    )

    paths = ProfilePaths(
        facts="data/profile/facts.yaml",
        summaries="data/profile/variants.yaml",
        application_answers="data/profile/application_answers.yaml",
        company_rules="data/profile/company_rules.yaml",
        learned_rules="data/profile/learned_rules.yaml",
        writing_samples="data/profile/writing_samples.md",
    )

    return Profile(
        person=person,
        level_policy=level_policy,
        role_families=role_families,
        bases=bases,
        location_policy=location_policy,
        scoring=scoring,
        paths=paths,
        candidate_blurb="",
    )
