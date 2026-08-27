"""Pure, deterministic role catalog for Career Path. Zero DB/AI/network access -- every
function here is a pure function of its inputs, exactly like placement_path_rules.py's own
purity discipline, but this module is self-contained and imports nothing from
placement_path_rules.py (Career Path must not depend on Roadmap's code).

Two-tier design: a curated catalog of hand-authored role families below covers the most
common ~20 target roles with real, specific content. Anything else (a novel custom role, or
a catalog role not yet curated) falls through to career_path.ai_generation's AI-assisted
resolver, which uses the exact same RoleFamilyDef shape so downstream code never needs to
know which tier produced a given path.
"""

import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional

# The 5 fixed phases every Career Path is organized into, in display order.
PHASE_ORDER: List[str] = ["foundations", "core_skills", "projects", "interview_prep", "job_ready"]

PHASE_TITLES: Dict[str, str] = {
    "foundations": "Foundations",
    "core_skills": "Core Skills",
    "projects": "Projects",
    "interview_prep": "Interview Preparation",
    "job_ready": "Job Ready",
}

PHASE_OBJECTIVES: Dict[str, str] = {
    "foundations": "Build the baseline knowledge everything else depends on.",
    "core_skills": "Develop the specific skills this role is evaluated on.",
    "projects": "Prove your skills with real, portfolio-worthy work.",
    "interview_prep": "Practice explaining your work and reasoning under interview conditions.",
    "job_ready": "Polish your resume and profile, then confirm you're ready to apply.",
}

# The 13 fixed browse categories from the product spec.
CATEGORY_LIST: List[str] = [
    "Technology",
    "Data & AI",
    "Business",
    "Finance",
    "Design",
    "Engineering",
    "Healthcare",
    "Marketing",
    "Operations",
    "Product",
    "Government",
    "Research",
    "Other",
]


@dataclass(frozen=True)
class SkillTemplate:
    skill_key: str
    title: str
    level: str = "beginner"  # beginner | intermediate | advanced


@dataclass(frozen=True)
class ProjectTemplate:
    project_key: str
    title: str
    difficulty: str  # beginner | intermediate | advanced
    skills_demonstrated: List[str]
    technologies: List[str]
    problem_statement: str
    expected_outcome: str
    what_should_be_included: str
    why_recruiters_value: str
    resume_bullet_suggestion: str
    interview_questions: List[str]


@dataclass(frozen=True)
class RoleFamilyDef:
    family_key: str
    display_name: str
    category: str
    aliases: FrozenSet[str]
    short_explanation: str
    responsibilities: List[str]
    core_skills: List[str]
    tools: List[str]
    difficulty: str  # beginner-friendly | moderate | challenging
    entry_level_expectations: str
    interview_focus: List[str]
    portfolio_expectations: str
    phase_skill_templates: Dict[str, List[SkillTemplate]]
    project_templates: List[ProjectTemplate]


# --------------------------------------------------------------------------------------
# Curated role families. Land the highest-value ~8 first (spanning tech/data/design/
# business/ops, to prove the abstraction generalizes) and backfill the rest of the ~20
# incrementally -- disclosed in the feature's final report as a v1 scope decision.
# --------------------------------------------------------------------------------------

ROLE_FAMILIES: Dict[str, RoleFamilyDef] = {}


def _register(family: RoleFamilyDef) -> None:
    ROLE_FAMILIES[family.family_key] = family


_register(
    RoleFamilyDef(
        family_key="sde",
        display_name="Software Engineer",
        category="Technology",
        aliases=frozenset(
            {
                "software engineer",
                "software developer",
                "sde",
                "swe",
                "backend developer",
                "backend engineer",
                "frontend developer",
                "frontend engineer",
                "full stack developer",
                "fullstack developer",
                "full stack engineer",
                "web developer",
                "application developer",
                "programmer",
            }
        ),
        short_explanation=(
            "Designs, builds, tests, and maintains software applications and systems, "
            "turning requirements into working, reliable code."
        ),
        responsibilities=[
            "Write clean, maintainable code to implement features and fix bugs",
            "Design data structures and APIs for new functionality",
            "Write and run tests to catch issues before release",
            "Review teammates' code and participate in code reviews",
            "Debug production issues and improve system reliability",
        ],
        core_skills=[
            "Data Structures & Algorithms",
            "Object-Oriented Programming",
            "Git & Version Control",
            "REST API Design",
            "Databases (SQL)",
            "Problem Solving",
            "Testing & Debugging",
        ],
        tools=["Git/GitHub", "VS Code or IntelliJ", "Postman", "Docker (basics)", "PostgreSQL/MySQL"],
        difficulty="moderate",
        entry_level_expectations=(
            "Comfortable writing working code in at least one language, understands core DSA "
            "well enough to solve medium-difficulty problems, has built and deployed at least "
            "one non-trivial project end-to-end."
        ),
        interview_focus=[
            "Data structures & algorithms (arrays, trees, graphs, DP)",
            "System design basics",
            "Language fundamentals & OOP",
            "Past project deep-dives",
            "Behavioral / teamwork questions",
        ],
        portfolio_expectations=(
            "2-3 projects on GitHub with clean READMEs, at least one deployed and publicly "
            "accessible, showing real problem-solving rather than tutorial clones."
        ),
        phase_skill_templates={
            "foundations": [
                SkillTemplate("prog_fundamentals", "Programming Language Fundamentals", "beginner"),
                SkillTemplate("git_basics", "Git & Version Control", "beginner"),
                SkillTemplate("oop_basics", "Object-Oriented Programming", "beginner"),
            ],
            "core_skills": [
                SkillTemplate("dsa_arrays_strings", "Arrays, Strings & Hashing", "intermediate"),
                SkillTemplate("dsa_trees_graphs", "Trees, Graphs & Recursion", "intermediate"),
                SkillTemplate("databases_sql", "Databases & SQL", "intermediate"),
                SkillTemplate("rest_apis", "Building REST APIs", "intermediate"),
            ],
            "interview_prep": [
                SkillTemplate("dsa_interview_practice", "DSA Interview Practice", "advanced"),
                SkillTemplate("system_design_basics", "System Design Fundamentals", "advanced"),
                SkillTemplate("behavioral_prep", "Behavioral Interview Prep", "intermediate"),
            ],
            "job_ready": [
                SkillTemplate("resume_github_polish", "Resume & GitHub Polish", "intermediate"),
                SkillTemplate("mock_interview_loop", "Full Mock Interview Loop", "advanced"),
            ],
        },
        project_templates=[
            ProjectTemplate(
                project_key="task_manager_api",
                title="Personal Task Manager API",
                difficulty="beginner",
                skills_demonstrated=["REST APIs", "Databases", "CRUD", "Authentication"],
                technologies=["Python or Node.js", "PostgreSQL"],
                problem_statement=("Build a backend API to create, read, update, and delete tasks with user authentication."),
                expected_outcome="A working, deployed REST API with documented endpoints.",
                what_should_be_included=("Auth, CRUD endpoints, input validation, and a README with setup instructions."),
                why_recruiters_value=(
                    "Shows you can build a complete, working backend service -- the most common junior SDE task."
                ),
                resume_bullet_suggestion=(
                    "Built a task management REST API with JWT authentication and PostgreSQL, deployed on a cloud platform."
                ),
                interview_questions=[
                    "Walk me through your API's authentication flow.",
                    "How did you handle validation errors?",
                    "What would you change if this had 10,000 users?",
                ],
            ),
            ProjectTemplate(
                project_key="url_shortener",
                title="URL Shortener with Analytics",
                difficulty="intermediate",
                skills_demonstrated=["System Design", "Databases", "Caching"],
                technologies=["Node.js or Java", "Redis", "PostgreSQL"],
                problem_statement="Build a URL shortening service that tracks click analytics.",
                expected_outcome="A scalable shortener handling redirects with basic click tracking.",
                what_should_be_included=(
                    "Collision-safe short-code generation, a redirect endpoint, click-count analytics, and rate limiting."
                ),
                why_recruiters_value=(
                    "Demonstrates system-design thinking beyond CRUD -- the classic 'design a "
                    "URL shortener' interview question, actually built."
                ),
                resume_bullet_suggestion=(
                    "Designed and built a URL shortener handling redirects with Redis caching and click analytics."
                ),
                interview_questions=[
                    "How do you generate unique short codes at scale?",
                    "How would you shard this across multiple servers?",
                    "How did you handle cache invalidation?",
                ],
            ),
            ProjectTemplate(
                project_key="realtime_chat",
                title="Real-Time Chat Application",
                difficulty="advanced",
                skills_demonstrated=["WebSockets", "System Design", "Scalability"],
                technologies=["Node.js or Go", "WebSockets", "Redis pub/sub"],
                problem_statement="Build a real-time chat app supporting multiple rooms and online presence.",
                expected_outcome="A working chat app with real-time message delivery and presence indicators.",
                what_should_be_included=(
                    "WebSocket connection handling, room management, message persistence, and online/offline status."
                ),
                why_recruiters_value=(
                    "Real-time systems are a strong signal of engineering maturity -- connection "
                    "handling, concurrency, and scale all show up here."
                ),
                resume_bullet_suggestion=(
                    "Built a real-time chat application supporting multiple rooms using "
                    "WebSockets and Redis pub/sub for horizontal scaling."
                ),
                interview_questions=[
                    "How do you handle a server restart without dropping connections?",
                    "How would you scale this to multiple server instances?",
                    "How do you handle message ordering guarantees?",
                ],
            ),
        ],
    )
)

_register(
    RoleFamilyDef(
        family_key="data_analyst",
        display_name="Data Analyst",
        category="Data & AI",
        aliases=frozenset({"data analyst", "business intelligence analyst", "bi analyst", "reporting analyst"}),
        short_explanation="Turns raw business data into clear insights and dashboards that drive decisions.",
        responsibilities=[
            "Query databases to answer business questions",
            "Clean and prepare data for analysis",
            "Build dashboards and reports for stakeholders",
            "Perform statistical analysis to find trends and patterns",
            "Present findings clearly to non-technical audiences",
        ],
        core_skills=[
            "SQL",
            "Excel",
            "Python (Pandas)",
            "Statistics",
            "Data Visualization",
            "Power BI or Tableau",
            "Business Communication",
        ],
        tools=["SQL (PostgreSQL/MySQL)", "Excel", "Power BI or Tableau", "Python (Pandas, Matplotlib)"],
        difficulty="beginner-friendly",
        entry_level_expectations=(
            "Can write SQL queries with joins and aggregations, build a clean dashboard, and "
            "explain what the numbers mean to someone non-technical."
        ),
        interview_focus=[
            "SQL query writing (live, on a shared screen)",
            "Case studies: 'how would you investigate a metric drop'",
            "Dashboard/portfolio walkthrough",
            "Statistics fundamentals",
            "Business communication",
        ],
        portfolio_expectations=(
            "1-2 end-to-end analyses with a real dataset, a public dashboard, and a short "
            "write-up of the business insight found -- not just a chart gallery."
        ),
        phase_skill_templates={
            "foundations": [
                SkillTemplate("excel_fundamentals", "Excel Fundamentals", "beginner"),
                SkillTemplate("sql_select_where", "SQL: SELECT, WHERE, ORDER BY", "beginner"),
                SkillTemplate("stats_basics", "Descriptive Statistics", "beginner"),
            ],
            "core_skills": [
                SkillTemplate("sql_joins_aggregations", "SQL: Joins & Aggregations", "intermediate"),
                SkillTemplate("python_pandas", "Data Analysis with Python (Pandas)", "intermediate"),
                SkillTemplate("data_visualization", "Data Visualization Principles", "intermediate"),
                SkillTemplate("bi_tool", "Power BI or Tableau", "intermediate"),
            ],
            "interview_prep": [
                SkillTemplate("sql_case_studies", "SQL Case Study Practice", "advanced"),
                SkillTemplate("business_case_prep", "Business Case Interview Prep", "intermediate"),
            ],
            "job_ready": [
                SkillTemplate("dashboard_portfolio_polish", "Dashboard Portfolio Polish", "intermediate"),
                SkillTemplate("mock_interview_loop", "Full Mock Interview Loop", "advanced"),
            ],
        },
        project_templates=[
            ProjectTemplate(
                project_key="sales_dashboard",
                title="Sales Performance Dashboard",
                difficulty="beginner",
                skills_demonstrated=["SQL", "Power BI/Tableau", "Data Cleaning"],
                technologies=["Power BI or Tableau", "SQL"],
                problem_statement="Build an interactive dashboard tracking sales performance across regions and time.",
                expected_outcome="A published, interactive dashboard with filters for region, product, and date.",
                what_should_be_included=(
                    "Cleaned source data, KPI cards (revenue, growth), trend charts, and a one-page insight summary."
                ),
                why_recruiters_value=(
                    "Dashboards are the most common analyst deliverable -- this shows you can ship one end-to-end."
                ),
                resume_bullet_suggestion=(
                    "Built an interactive Power BI dashboard tracking sales KPIs across 5 regions, surfacing a 12% regional "
                    "growth gap."
                ),
                interview_questions=[
                    "Walk me through how you'd investigate a sudden sales drop in one region.",
                    "How did you decide which KPIs to show?",
                    "What would you add if a stakeholder asked for a forecast?",
                ],
            ),
            ProjectTemplate(
                project_key="customer_churn_analysis",
                title="Customer Churn Analysis",
                difficulty="intermediate",
                skills_demonstrated=["Python", "Statistics", "Feature Analysis"],
                technologies=["Python (Pandas)", "SQL", "Matplotlib/Seaborn"],
                problem_statement=(
                    "Analyze customer data to identify why customers are leaving and which segments are highest risk."
                ),
                expected_outcome="A written analysis identifying the top 3 churn drivers with supporting charts.",
                what_should_be_included=(
                    "Data cleaning, exploratory analysis, segment comparison, and clear, non-technical conclusions."
                ),
                why_recruiters_value="Shows you can go beyond charts to a real business recommendation grounded in data.",
                resume_bullet_suggestion=(
                    "Analyzed customer churn patterns across 10K+ records, identifying 3 key risk factors and proposing a "
                    "retention strategy."
                ),
                interview_questions=[
                    "How did you validate that your churn drivers were statistically meaningful?",
                    "What data would have made this analysis stronger?",
                    "How would you turn this into a recurring dashboard?",
                ],
            ),
            ProjectTemplate(
                project_key="end_to_end_analytics_platform",
                title="End-to-End Analytics Platform",
                difficulty="advanced",
                skills_demonstrated=["SQL", "Python", "ETL", "Dashboarding"],
                technologies=["Python", "SQL", "Power BI/Tableau"],
                problem_statement=(
                    "Build a full pipeline: ingest raw data, clean and transform it, and surface it in a live dashboard."
                ),
                expected_outcome="A reproducible pipeline from raw data to a refreshed dashboard.",
                what_should_be_included=(
                    "An ETL script, a documented data model, and a dashboard connected to the transformed output."
                ),
                why_recruiters_value="Proves you understand the full analytics lifecycle, not just the visualization step.",
                resume_bullet_suggestion=(
                    "Built an end-to-end analytics pipeline transforming raw data into a live dashboard, reducing manual "
                    "reporting time."
                ),
                interview_questions=[
                    "How would this pipeline handle a schema change in the source data?",
                    "How did you ensure data quality at each step?",
                    "How would you schedule this to run automatically?",
                ],
            ),
        ],
    )
)

_register(
    RoleFamilyDef(
        family_key="data_scientist",
        display_name="Data Scientist",
        category="Data & AI",
        aliases=frozenset({"data scientist", "applied scientist", "quantitative analyst", "decision scientist"}),
        short_explanation=(
            "Uses statistics and machine learning to answer complex questions and build predictive models from data."
        ),
        responsibilities=[
            "Formulate business problems as data science questions",
            "Clean and explore large, messy datasets",
            "Build and validate predictive/statistical models",
            "Communicate model results and limitations to stakeholders",
            "Collaborate with engineers to productionize models",
        ],
        core_skills=[
            "Python",
            "Statistics & Probability",
            "Machine Learning",
            "SQL",
            "Data Wrangling",
            "Experimentation (A/B testing)",
            "Communication",
        ],
        tools=["Python (Pandas, scikit-learn)", "Jupyter", "SQL", "Git"],
        difficulty="challenging",
        entry_level_expectations=(
            "Solid Python and statistics fundamentals, has trained and evaluated at least a "
            "few ML models end-to-end, can explain model results in plain language."
        ),
        interview_focus=[
            "Statistics & probability (often live problems)",
            "ML fundamentals (bias/variance, evaluation metrics)",
            "Case studies / product-sense questions",
            "Past project deep-dives",
            "SQL and Python coding",
        ],
        portfolio_expectations=(
            "2-3 modeling projects with clear problem framing, honest evaluation (not just "
            "accuracy), and a write-up explaining the 'why', not just the code."
        ),
        phase_skill_templates={
            "foundations": [
                SkillTemplate("python_for_ds", "Python for Data Science", "beginner"),
                SkillTemplate("stats_probability", "Statistics & Probability", "beginner"),
                SkillTemplate("sql_for_ds", "SQL for Data Science", "beginner"),
            ],
            "core_skills": [
                SkillTemplate("data_wrangling", "Data Wrangling & Cleaning", "intermediate"),
                SkillTemplate("ml_fundamentals", "Machine Learning Fundamentals", "intermediate"),
                SkillTemplate("model_evaluation", "Model Evaluation & Metrics", "intermediate"),
                SkillTemplate("ab_testing", "Experimentation & A/B Testing", "intermediate"),
            ],
            "interview_prep": [
                SkillTemplate("case_study_prep", "Product/Case Study Interview Prep", "advanced"),
                SkillTemplate("ml_theory_review", "ML Theory Review", "advanced"),
            ],
            "job_ready": [
                SkillTemplate("portfolio_writeups", "Project Write-Up Polish", "intermediate"),
                SkillTemplate("mock_interview_loop", "Full Mock Interview Loop", "advanced"),
            ],
        },
        project_templates=[
            ProjectTemplate(
                project_key="house_price_prediction",
                title="House Price Prediction Model",
                difficulty="beginner",
                skills_demonstrated=["Regression", "Feature Engineering", "Model Evaluation"],
                technologies=["Python", "scikit-learn", "Pandas"],
                problem_statement="Predict house prices from a structured dataset of property features.",
                expected_outcome=(
                    "A trained regression model with a documented evaluation (RMSE/R-squared) and feature importance."
                ),
                what_should_be_included=(
                    "Data cleaning, feature engineering, a baseline model, and a tuned model with comparison."
                ),
                why_recruiters_value="Shows the fundamentals of a supervised learning workflow, done properly.",
                resume_bullet_suggestion=(
                    "Built a regression model predicting house prices, improving RMSE by 18% over baseline through feature "
                    "engineering."
                ),
                interview_questions=[
                    "Why did you choose this evaluation metric?",
                    "How did you handle outliers in the data?",
                    "What would overfitting look like here, and how did you check for it?",
                ],
            ),
            ProjectTemplate(
                project_key="customer_segmentation",
                title="Customer Segmentation with Clustering",
                difficulty="intermediate",
                skills_demonstrated=["Unsupervised Learning", "Feature Engineering", "Business Interpretation"],
                technologies=["Python", "scikit-learn"],
                problem_statement="Segment customers into meaningful groups based on behavior data.",
                expected_outcome="A set of customer segments with clear, business-interpretable profiles.",
                what_should_be_included=(
                    "Feature selection, a clustering approach with justification, and named/described segments."
                ),
                why_recruiters_value=(
                    "Unsupervised problems test judgment, not just technique -- there's no single right answer to validate "
                    "against."
                ),
                resume_bullet_suggestion=(
                    "Segmented 50K+ customers into 5 behavioral clusters using K-Means, informing a targeted marketing strategy."
                ),
                interview_questions=[
                    "How did you choose the number of clusters?",
                    "How would you validate that these segments are meaningful?",
                    "How would you turn this into a live scoring system?",
                ],
            ),
            ProjectTemplate(
                project_key="churn_prediction_deployed",
                title="Churn Prediction Model (Deployed)",
                difficulty="advanced",
                skills_demonstrated=["Classification", "Model Deployment", "Monitoring"],
                technologies=["Python", "scikit-learn", "Flask/FastAPI"],
                problem_statement="Build a churn classifier and expose it as a simple API for real-time predictions.",
                expected_outcome="A deployed prediction endpoint with a documented precision/recall tradeoff discussion.",
                what_should_be_included=(
                    "A trained classifier, a served API endpoint, and a note on how you'd monitor it in production."
                ),
                why_recruiters_value=(
                    "Bridges data science and engineering -- a model that's actually usable, not just a notebook."
                ),
                resume_bullet_suggestion=(
                    "Deployed a churn prediction model as a REST API, achieving 0.82 precision at a business-appropriate recall "
                    "threshold."
                ),
                interview_questions=[
                    "How did you pick your classification threshold?",
                    "How would you detect model drift after deployment?",
                    "What would you monitor once this is live?",
                ],
            ),
        ],
    )
)

_register(
    RoleFamilyDef(
        family_key="ml_engineer",
        display_name="Machine Learning Engineer",
        category="Data & AI",
        aliases=frozenset(
            {
                "ml engineer",
                "machine learning engineer",
                "machine learning developer",
                "applied ml engineer",
                "ai engineer",
                "mlops engineer",
            }
        ),
        short_explanation=(
            "Builds and productionizes machine learning systems -- taking models from notebooks to reliable, scalable services."
        ),
        responsibilities=[
            "Design and train ML models for production use cases",
            "Build data and training pipelines",
            "Deploy models as scalable, monitored services",
            "Optimize model performance and inference latency",
            "Collaborate with data scientists and backend engineers",
        ],
        core_skills=[
            "Python",
            "Machine Learning",
            "Software Engineering",
            "Model Deployment (MLOps)",
            "Data Pipelines",
            "Cloud Basics",
            "System Design for ML",
        ],
        tools=["Python (PyTorch/TensorFlow, scikit-learn)", "Docker", "Git", "A cloud platform (AWS/GCP/Azure)"],
        difficulty="challenging",
        entry_level_expectations=(
            "Strong Python and software-engineering fundamentals plus core ML knowledge; has "
            "trained a model and deployed it as a working service, not just a notebook."
        ),
        interview_focus=[
            "ML fundamentals & math intuition",
            "Coding (Python, DSA)",
            "ML system design (e.g. 'design a recommendation system')",
            "MLOps: deployment, monitoring, retraining",
            "Past project deep-dives",
        ],
        portfolio_expectations=(
            "At least one project that goes end-to-end from data to a deployed, callable "
            "model -- deployment quality matters as much as model accuracy here."
        ),
        phase_skill_templates={
            "foundations": [
                SkillTemplate("python_swe_fundamentals", "Python & Software Engineering Fundamentals", "beginner"),
                SkillTemplate("ml_math_intuition", "ML Math Intuition (Linear Algebra, Calculus)", "beginner"),
                SkillTemplate("ml_fundamentals", "Machine Learning Fundamentals", "beginner"),
            ],
            "core_skills": [
                SkillTemplate("deep_learning_basics", "Deep Learning Basics", "intermediate"),
                SkillTemplate("data_pipelines", "Data & Training Pipelines", "intermediate"),
                SkillTemplate("model_deployment", "Model Deployment (Docker, APIs)", "intermediate"),
                SkillTemplate("cloud_basics", "Cloud Platform Basics", "intermediate"),
            ],
            "interview_prep": [
                SkillTemplate("ml_system_design", "ML System Design Practice", "advanced"),
                SkillTemplate("coding_dsa_for_mle", "Coding & DSA Practice", "advanced"),
            ],
            "job_ready": [
                SkillTemplate("portfolio_deployment_polish", "Deployment Polish & Documentation", "intermediate"),
                SkillTemplate("mock_interview_loop", "Full Mock Interview Loop", "advanced"),
            ],
        },
        project_templates=[
            ProjectTemplate(
                project_key="image_classifier_api",
                title="Image Classifier API",
                difficulty="beginner",
                skills_demonstrated=["Deep Learning", "Model Serving", "APIs"],
                technologies=["Python", "PyTorch/TensorFlow", "FastAPI"],
                problem_statement="Train an image classifier and serve it behind a REST API.",
                expected_outcome="A working API that accepts an image and returns a classification with confidence.",
                what_should_be_included="A trained model, an inference endpoint, and basic input validation/error handling.",
                why_recruiters_value=(
                    "The most common 'first deployed model' project -- shows you can close the loop from training to serving."
                ),
                resume_bullet_suggestion=(
                    "Trained and deployed an image classification model as a REST API using PyTorch and FastAPI."
                ),
                interview_questions=[
                    "How did you handle invalid or unexpected input images?",
                    "How would you reduce inference latency?",
                    "How would you retrain this model as new data arrives?",
                ],
            ),
            ProjectTemplate(
                project_key="recommendation_engine",
                title="Recommendation Engine",
                difficulty="intermediate",
                skills_demonstrated=["Collaborative Filtering", "Data Pipelines", "Evaluation"],
                technologies=["Python", "scikit-learn/Surprise", "SQL"],
                problem_statement="Build a recommendation system for a product or content catalog.",
                expected_outcome="A working recommender producing ranked suggestions, with an offline evaluation.",
                what_should_be_included=(
                    "A data pipeline, a recommendation algorithm, and an evaluation methodology (e.g. precision@k)."
                ),
                why_recruiters_value=(
                    "Recommendation systems are a very common real-world ML system and a frequent design-interview topic."
                ),
                resume_bullet_suggestion=(
                    "Built a collaborative-filtering recommendation engine, achieving a measurable lift in precision@10 over a "
                    "popularity baseline."
                ),
                interview_questions=[
                    "How did you evaluate recommendation quality offline?",
                    "How would you handle the cold-start problem for new users?",
                    "How would this scale to millions of users?",
                ],
            ),
            ProjectTemplate(
                project_key="ml_pipeline_with_monitoring",
                title="End-to-End ML Pipeline with Monitoring",
                difficulty="advanced",
                skills_demonstrated=["MLOps", "CI/CD for ML", "Monitoring"],
                technologies=["Python", "Docker", "A cloud platform"],
                problem_statement="Build a full pipeline: data ingestion, training, deployment, and basic drift monitoring.",
                expected_outcome="A documented, reproducible pipeline with a monitoring dashboard or logging for model health.",
                what_should_be_included=(
                    "An automated training step, a deployed model, and a way to detect when predictions drift from expected "
                    "patterns."
                ),
                why_recruiters_value=(
                    "Shows you understand ML in production is a lifecycle, not a one-time deployment -- a key MLE differentiator."
                ),
                resume_bullet_suggestion=(
                    "Built an end-to-end ML pipeline with automated retraining and drift monitoring, reducing manual "
                    "intervention."
                ),
                interview_questions=[
                    "How do you detect data or concept drift?",
                    "How would you roll back a bad model deployment?",
                    "What would trigger an automatic retrain in your system?",
                ],
            ),
        ],
    )
)

_register(
    RoleFamilyDef(
        family_key="ui_ux_designer",
        display_name="UI/UX Designer",
        category="Design",
        aliases=frozenset(
            {"ui/ux designer", "ui ux designer", "ux designer", "ui designer", "product designer", "interaction designer"}
        ),
        short_explanation=(
            "Designs digital products that are usable, accessible, and pleasant -- from user research through polished "
            "interfaces."
        ),
        responsibilities=[
            "Research user needs and pain points",
            "Create wireframes, prototypes, and user flows",
            "Design visual interfaces consistent with a design system",
            "Run usability tests and iterate on feedback",
            "Collaborate closely with product managers and engineers",
        ],
        core_skills=[
            "User Research",
            "Wireframing & Prototyping",
            "Visual Design",
            "Design Systems",
            "Usability Testing",
            "Figma",
            "Communication & Storytelling",
        ],
        tools=["Figma", "FigJam or Miro", "A prototyping tool", "Basic HTML/CSS awareness"],
        difficulty="beginner-friendly",
        entry_level_expectations=(
            "Can take a problem from research through wireframes to a polished, interactive "
            "prototype, and can clearly explain design decisions."
        ),
        interview_focus=[
            "Portfolio / case study walkthrough (the primary interview format)",
            "Design critique exercises",
            "Whiteboard/collaborative design challenges",
            "Process & rationale questions ('why' over 'what')",
            "Behavioral / collaboration questions",
        ],
        portfolio_expectations=(
            "2-3 detailed case studies (not just polished screens) showing the problem, "
            "research, iterations, and outcome -- this matters more than DSA/coding here."
        ),
        phase_skill_templates={
            "foundations": [
                SkillTemplate("design_fundamentals", "Design Fundamentals (Layout, Color, Typography)", "beginner"),
                SkillTemplate("figma_basics", "Figma Basics", "beginner"),
                SkillTemplate("ux_research_basics", "User Research Basics", "beginner"),
            ],
            "core_skills": [
                SkillTemplate("wireframing_prototyping", "Wireframing & Prototyping", "intermediate"),
                SkillTemplate("design_systems", "Design Systems", "intermediate"),
                SkillTemplate("usability_testing", "Usability Testing", "intermediate"),
                SkillTemplate("interaction_design", "Interaction & Visual Design", "intermediate"),
            ],
            "interview_prep": [
                SkillTemplate("case_study_storytelling", "Case Study Storytelling", "advanced"),
                SkillTemplate("design_critique_practice", "Design Critique Practice", "advanced"),
            ],
            "job_ready": [
                SkillTemplate("portfolio_polish", "Portfolio Case Study Polish", "intermediate"),
                SkillTemplate("mock_interview_loop", "Full Mock Interview Loop", "advanced"),
            ],
        },
        project_templates=[
            ProjectTemplate(
                project_key="app_redesign_case_study",
                title="App Redesign Case Study",
                difficulty="beginner",
                skills_demonstrated=["User Research", "Wireframing", "Visual Design"],
                technologies=["Figma"],
                problem_statement="Pick an app with a real usability problem and redesign one core flow.",
                expected_outcome="A documented case study: problem, research, wireframes, final designs, and rationale.",
                what_should_be_included=(
                    "Problem framing, 2-3 user pain points, wireframe iterations, and a polished final prototype."
                ),
                why_recruiters_value=(
                    "Case studies are how UX hiring actually works -- this is the standard first portfolio piece."
                ),
                resume_bullet_suggestion=(
                    "Redesigned the checkout flow of a mobile app, reducing wireframed steps from 5 to 3 based on usability "
                    "findings."
                ),
                interview_questions=[
                    "What was the core usability problem you identified?",
                    "Walk me through how your design evolved between iterations.",
                    "How would you validate this redesign with real users?",
                ],
            ),
            ProjectTemplate(
                project_key="design_system_project",
                title="Mini Design System",
                difficulty="intermediate",
                skills_demonstrated=["Design Systems", "Component Design", "Consistency"],
                technologies=["Figma"],
                problem_statement="Build a small, reusable design system (components, tokens, guidelines) for a product.",
                expected_outcome="A documented component library usable across multiple screens.",
                what_should_be_included="Color/type tokens, core components (buttons, inputs, cards), and usage guidelines.",
                why_recruiters_value="Shows systems thinking, not just single-screen polish -- valuable at any team size.",
                resume_bullet_suggestion=(
                    "Built a 20+ component design system with documented usage guidelines, applied across 3 product screens."
                ),
                interview_questions=[
                    "How did you decide what belonged in the system vs. one-off designs?",
                    "How would you roll this out to an existing product?",
                    "How do you keep a design system consistent as a product grows?",
                ],
            ),
            ProjectTemplate(
                project_key="end_to_end_product_case_study",
                title="End-to-End Product Case Study",
                difficulty="advanced",
                skills_demonstrated=["User Research", "Usability Testing", "Full Design Process"],
                technologies=["Figma", "A usability testing tool"],
                problem_statement="Design a new product/feature from scratch, including real (or simulated) user testing.",
                expected_outcome="A full case study from problem discovery through a tested, iterated final design.",
                what_should_be_included=(
                    "Research findings, personas or user needs, multiple design iterations, and testing results that changed the "
                    "design."
                ),
                why_recruiters_value=(
                    "Demonstrates the complete design process end-to-end -- the strongest signal for mid-level roles."
                ),
                resume_bullet_suggestion=(
                    "Led end-to-end design of a new feature, running 5 usability tests that drove 2 major design iterations."
                ),
                interview_questions=[
                    "What did your usability testing reveal that you didn't expect?",
                    "How did you prioritize which feedback to act on?",
                    "How would you measure this design's success after launch?",
                ],
            ),
        ],
    )
)

_register(
    RoleFamilyDef(
        family_key="business_analyst",
        display_name="Business Analyst",
        category="Business",
        aliases=frozenset({"business analyst", "ba", "business systems analyst", "process analyst"}),
        short_explanation=(
            "Bridges business needs and technical solutions -- gathering requirements, "
            "analyzing processes, and driving decisions with data."
        ),
        responsibilities=[
            "Gather and document business requirements",
            "Analyze processes and identify improvement opportunities",
            "Translate business needs into specs for technical teams",
            "Build reports and dashboards to support decisions",
            "Facilitate stakeholder communication across teams",
        ],
        core_skills=[
            "Requirements Gathering",
            "Process Mapping",
            "SQL",
            "Excel",
            "Data Analysis",
            "Stakeholder Communication",
            "Documentation",
        ],
        tools=["Excel", "SQL", "A BI tool (Power BI/Tableau)", "A process-mapping tool (Visio/Lucidchart)"],
        difficulty="beginner-friendly",
        entry_level_expectations=(
            "Can gather requirements from a stakeholder, translate them into a clear "
            "document, and back up a recommendation with basic data analysis."
        ),
        interview_focus=[
            "Case studies ('how would you approach this business problem')",
            "SQL/Excel exercises",
            "Requirements-gathering role-play",
            "Communication & stakeholder-management questions",
            "Past project deep-dives",
        ],
        portfolio_expectations=(
            "1-2 case studies showing a business problem, your analysis, and a clear "
            "recommendation -- process documentation counts as much as dashboards here."
        ),
        phase_skill_templates={
            "foundations": [
                SkillTemplate("requirements_gathering", "Requirements Gathering Basics", "beginner"),
                SkillTemplate("excel_fundamentals", "Excel Fundamentals", "beginner"),
                SkillTemplate("process_mapping", "Process Mapping", "beginner"),
            ],
            "core_skills": [
                SkillTemplate("sql_for_ba", "SQL for Business Analysis", "intermediate"),
                SkillTemplate("data_analysis_ba", "Data Analysis & Reporting", "intermediate"),
                SkillTemplate("bi_tool_ba", "BI Tool (Power BI/Tableau)", "intermediate"),
                SkillTemplate("documentation_specs", "Writing Specs & Documentation", "intermediate"),
            ],
            "interview_prep": [
                SkillTemplate("case_study_prep_ba", "Business Case Study Prep", "advanced"),
                SkillTemplate("stakeholder_communication", "Stakeholder Communication Practice", "intermediate"),
            ],
            "job_ready": [
                SkillTemplate("portfolio_case_studies_ba", "Case Study Portfolio Polish", "intermediate"),
                SkillTemplate("mock_interview_loop", "Full Mock Interview Loop", "advanced"),
            ],
        },
        project_templates=[
            ProjectTemplate(
                project_key="process_improvement_case_study",
                title="Process Improvement Case Study",
                difficulty="beginner",
                skills_demonstrated=["Process Mapping", "Requirements Gathering"],
                technologies=["Visio/Lucidchart", "Excel"],
                problem_statement="Pick an inefficient process (real or hypothetical) and document a redesigned version.",
                expected_outcome="A before/after process map with a clear explanation of the improvement and its impact.",
                what_should_be_included=(
                    "A current-state map, identified bottlenecks, a proposed future-state map, and estimated impact."
                ),
                why_recruiters_value=(
                    "Process analysis is core BA work -- this proves you can structure and improve a real workflow."
                ),
                resume_bullet_suggestion=(
                    "Documented and redesigned a business process, identifying 3 bottlenecks and proposing a 30% cycle-time "
                    "reduction."
                ),
                interview_questions=[
                    "How did you identify the bottlenecks?",
                    "How would you validate your proposed improvement before implementing it?",
                    "Who would you need buy-in from to make this change?",
                ],
            ),
            ProjectTemplate(
                project_key="sales_reporting_dashboard_ba",
                title="Business Reporting Dashboard",
                difficulty="intermediate",
                skills_demonstrated=["SQL", "Data Analysis", "Dashboarding"],
                technologies=["SQL", "Power BI/Tableau"],
                problem_statement="Build a recurring report/dashboard answering a specific business question.",
                expected_outcome="A dashboard with a written recommendation based on the findings.",
                what_should_be_included=(
                    "A clear business question, the SQL/analysis behind it, the dashboard, and a recommendation."
                ),
                why_recruiters_value="Shows you can move from a business question to data to an actionable recommendation.",
                resume_bullet_suggestion=(
                    "Built a recurring sales reporting dashboard, surfacing a pricing insight that informed a strategy "
                    "recommendation."
                ),
                interview_questions=[
                    "How did you decide which metrics mattered?",
                    "What would you do if the data contradicted stakeholder expectations?",
                    "How would you keep this dashboard useful as the business changes?",
                ],
            ),
            ProjectTemplate(
                project_key="requirements_to_spec",
                title="Requirements-to-Spec Case Study",
                difficulty="advanced",
                skills_demonstrated=["Requirements Gathering", "Documentation", "Stakeholder Management"],
                technologies=["A documentation tool", "Excel"],
                problem_statement="Simulate gathering requirements for a new feature and write a full specification document.",
                expected_outcome="A complete requirements document ready to hand to an engineering team.",
                what_should_be_included=(
                    "Stakeholder needs, functional/non-functional requirements, edge cases, and acceptance criteria."
                ),
                why_recruiters_value="This is the exact deliverable BAs are hired to produce -- a strong signal of readiness.",
                resume_bullet_suggestion=(
                    "Authored a full requirements specification for a new feature, covering functional requirements and "
                    "acceptance criteria."
                ),
                interview_questions=[
                    "How did you handle conflicting requirements from different stakeholders?",
                    "How did you decide what was in scope vs. out of scope?",
                    "How would engineering push back on this spec, and how would you respond?",
                ],
            ),
        ],
    )
)

_register(
    RoleFamilyDef(
        family_key="devops_engineer",
        display_name="DevOps / Cloud Engineer",
        category="Technology",
        aliases=frozenset(
            {
                "devops engineer",
                "cloud engineer",
                "site reliability engineer",
                "sre",
                "infrastructure engineer",
                "platform engineer",
            }
        ),
        short_explanation=(
            "Builds and maintains the infrastructure, pipelines, and automation that let software teams ship reliably and at "
            "scale."
        ),
        responsibilities=[
            "Build and maintain CI/CD pipelines",
            "Manage cloud infrastructure (compute, networking, storage)",
            "Automate deployment and configuration with infrastructure-as-code",
            "Monitor systems and respond to incidents",
            "Improve reliability, scalability, and cost efficiency",
        ],
        core_skills=[
            "Linux & Shell Scripting",
            "Cloud Platforms (AWS/GCP/Azure)",
            "CI/CD",
            "Containers (Docker/Kubernetes)",
            "Infrastructure as Code",
            "Networking Basics",
            "Monitoring & Observability",
        ],
        tools=["Linux", "Docker", "A cloud platform (AWS/GCP/Azure)", "Git", "Terraform (basics)"],
        difficulty="moderate",
        entry_level_expectations=(
            "Comfortable in a Linux shell, understands core networking, has set up a CI/CD "
            "pipeline and deployed a containerized app to the cloud at least once."
        ),
        interview_focus=[
            "Linux & shell scripting exercises",
            "Cloud architecture / troubleshooting scenarios",
            "CI/CD pipeline design",
            "Incident response / 'a service is down, what do you check' questions",
            "Past project deep-dives",
        ],
        portfolio_expectations=(
            "1-2 projects showing infrastructure-as-code and a working CI/CD pipeline for a "
            "real app, with clear documentation of the architecture."
        ),
        phase_skill_templates={
            "foundations": [
                SkillTemplate("linux_shell", "Linux & Shell Scripting", "beginner"),
                SkillTemplate("networking_basics", "Networking Basics", "beginner"),
                SkillTemplate("git_basics_devops", "Git & Version Control", "beginner"),
            ],
            "core_skills": [
                SkillTemplate("docker_containers", "Docker & Containers", "intermediate"),
                SkillTemplate("cicd_pipelines", "CI/CD Pipelines", "intermediate"),
                SkillTemplate("cloud_platform", "Cloud Platform Fundamentals", "intermediate"),
                SkillTemplate("infra_as_code", "Infrastructure as Code (Terraform)", "intermediate"),
            ],
            "interview_prep": [
                SkillTemplate("troubleshooting_scenarios", "Troubleshooting Scenario Practice", "advanced"),
                SkillTemplate("incident_response_prep", "Incident Response Prep", "advanced"),
            ],
            "job_ready": [
                SkillTemplate("portfolio_infra_docs", "Infrastructure Documentation Polish", "intermediate"),
                SkillTemplate("mock_interview_loop", "Full Mock Interview Loop", "advanced"),
            ],
        },
        project_templates=[
            ProjectTemplate(
                project_key="cicd_pipeline_project",
                title="CI/CD Pipeline for a Web App",
                difficulty="beginner",
                skills_demonstrated=["CI/CD", "Git", "Automated Testing"],
                technologies=["GitHub Actions/GitLab CI", "Docker"],
                problem_statement="Set up an automated pipeline that tests, builds, and deploys a sample web app on every push.",
                expected_outcome="A working pipeline that automatically deploys on merge to main.",
                what_should_be_included="Automated tests, a build step, a deploy step, and documentation of the pipeline stages.",
                why_recruiters_value=(
                    "CI/CD is foundational DevOps work -- this proves hands-on, not just conceptual, understanding."
                ),
                resume_bullet_suggestion=(
                    "Built a CI/CD pipeline automating test, build, and deploy for a web app, cutting manual deploy time to zero."
                ),
                interview_questions=[
                    "What happens in your pipeline if a test fails?",
                    "How would you add a rollback step?",
                    "How would you secure secrets used in this pipeline?",
                ],
            ),
            ProjectTemplate(
                project_key="containerized_app_deployment",
                title="Containerized App Deployment on the Cloud",
                difficulty="intermediate",
                skills_demonstrated=["Docker", "Cloud Platforms", "Networking"],
                technologies=["Docker", "AWS/GCP/Azure"],
                problem_statement="Containerize an application and deploy it to a cloud provider with proper networking.",
                expected_outcome="A publicly accessible, containerized app running on a cloud platform.",
                what_should_be_included="A Dockerfile, cloud deployment config, and documented networking/security setup.",
                why_recruiters_value="Shows the full path from code to a live, cloud-hosted service.",
                resume_bullet_suggestion=(
                    "Containerized and deployed a web application to AWS/GCP, configuring networking and basic auto-scaling."
                ),
                interview_questions=[
                    "How did you handle environment variables and secrets?",
                    "How would this scale under increased traffic?",
                    "What monitoring would you add before calling this production-ready?",
                ],
            ),
            ProjectTemplate(
                project_key="infra_as_code_project",
                title="Infrastructure as Code Project",
                difficulty="advanced",
                skills_demonstrated=["Terraform", "Cloud Architecture", "Automation"],
                technologies=["Terraform", "A cloud platform"],
                problem_statement="Define a small cloud environment entirely as code, reproducible from scratch.",
                expected_outcome=(
                    "A version-controlled infrastructure definition that can stand up the environment in one command."
                ),
                what_should_be_included=(
                    "Terraform modules, a documented architecture diagram, and a README explaining how to apply/destroy it."
                ),
                why_recruiters_value="Infrastructure-as-code is a core modern DevOps expectation, not a nice-to-have.",
                resume_bullet_suggestion=(
                    "Defined a cloud environment as Terraform infrastructure-as-code, enabling reproducible one-command "
                    "provisioning."
                ),
                interview_questions=[
                    "How do you manage Terraform state safely across a team?",
                    "How would you handle a partial apply failure?",
                    "How would you structure this for multiple environments (dev/staging/prod)?",
                ],
            ),
        ],
    )
)

_register(
    RoleFamilyDef(
        family_key="hr_recruiter",
        display_name="HR / Recruitment",
        category="Business",
        aliases=frozenset(
            {
                "hr",
                "human resources",
                "recruiter",
                "hr recruiter",
                "talent acquisition",
                "hr generalist",
                "hr analyst",
                "people operations",
            }
        ),
        short_explanation="Manages the people side of a business -- hiring, employee relations, policy, and workplace culture.",
        responsibilities=[
            "Source and screen candidates for open roles",
            "Coordinate interviews and manage the hiring pipeline",
            "Support onboarding and employee relations",
            "Apply labor law and company policy correctly",
            "Analyze HR metrics (attrition, time-to-hire, etc.)",
        ],
        core_skills=[
            "Recruitment Lifecycle",
            "Interviewing Techniques",
            "Labor Law Basics",
            "HR Analytics",
            "Communication",
            "Conflict Resolution",
            "HRIS Tools",
        ],
        tools=["An ATS (Applicant Tracking System)", "LinkedIn Recruiter", "Excel", "An HRIS platform"],
        difficulty="beginner-friendly",
        entry_level_expectations=(
            "Understands the end-to-end hiring process, can conduct a structured interview, "
            "and has basic awareness of labor-law fundamentals relevant to hiring."
        ),
        interview_focus=[
            "Behavioral / situational judgment questions",
            "Role-play: screening or interviewing a candidate",
            "HR policy / labor-law scenario questions",
            "HR metrics interpretation",
            "Communication & conflict-resolution questions",
        ],
        portfolio_expectations=(
            "A short portfolio isn't standard here; instead, prepare 2-3 detailed STAR-format "
            "stories and, where possible, a small HR-analytics exercise (e.g. an attrition "
            "analysis) to discuss."
        ),
        phase_skill_templates={
            "foundations": [
                SkillTemplate("recruitment_lifecycle_basics", "Recruitment Lifecycle Basics", "beginner"),
                SkillTemplate("labor_law_basics", "Labor Law Fundamentals", "beginner"),
                SkillTemplate("communication_basics_hr", "Professional Communication", "beginner"),
            ],
            "core_skills": [
                SkillTemplate("interviewing_techniques", "Structured Interviewing Techniques", "intermediate"),
                SkillTemplate("hr_analytics", "HR Analytics (Attrition, Time-to-Hire)", "intermediate"),
                SkillTemplate("conflict_resolution", "Conflict Resolution", "intermediate"),
                SkillTemplate("hris_tools", "HRIS / ATS Tools", "intermediate"),
            ],
            "interview_prep": [
                SkillTemplate("behavioral_hr_prep", "Behavioral & Situational Prep (STAR method)", "advanced"),
                SkillTemplate("policy_scenario_prep", "Policy Scenario Practice", "intermediate"),
            ],
            "job_ready": [
                SkillTemplate("star_stories_polish", "STAR Story Polish", "intermediate"),
                SkillTemplate("mock_interview_loop", "Full Mock Interview Loop", "advanced"),
            ],
        },
        project_templates=[
            ProjectTemplate(
                project_key="hiring_process_design",
                title="Hiring Process Design",
                difficulty="beginner",
                skills_demonstrated=["Recruitment Lifecycle", "Process Design"],
                technologies=["Excel", "A documentation tool"],
                problem_statement="Design a complete hiring process for a hypothetical role, from job post to offer.",
                expected_outcome="A documented hiring workflow with stage definitions and screening criteria.",
                what_should_be_included="Sourcing strategy, screening criteria, interview stages, and evaluation rubric.",
                why_recruiters_value=(
                    "Shows structured thinking about the core recruiter deliverable -- a repeatable hiring process."
                ),
                resume_bullet_suggestion=(
                    "Designed a structured 4-stage hiring process with defined screening criteria for a sample role."
                ),
                interview_questions=[
                    "How did you decide on your screening criteria?",
                    "How would you keep this process fair and unbiased?",
                    "How would you measure whether this process is working?",
                ],
            ),
            ProjectTemplate(
                project_key="attrition_analysis",
                title="Employee Attrition Analysis",
                difficulty="intermediate",
                skills_demonstrated=["HR Analytics", "Data Interpretation"],
                technologies=["Excel", "A public HR dataset"],
                problem_statement="Analyze a sample HR dataset to identify attrition patterns and drivers.",
                expected_outcome="A short report identifying top attrition drivers and a retention recommendation.",
                what_should_be_included="Attrition rate by segment, identified drivers, and 1-2 actionable recommendations.",
                why_recruiters_value=(
                    "HR analytics is an increasingly expected skill -- shows you go beyond process into data-informed decisions."
                ),
                resume_bullet_suggestion=(
                    "Analyzed employee attrition data, identifying 2 key drivers and proposing a retention initiative."
                ),
                interview_questions=[
                    "What attrition driver surprised you most?",
                    "How would you validate this finding with the business?",
                    "What would you track going forward to catch this earlier?",
                ],
            ),
            ProjectTemplate(
                project_key="onboarding_program_design",
                title="Onboarding Program Design",
                difficulty="advanced",
                skills_demonstrated=["Program Design", "Employee Experience"],
                technologies=["A documentation tool"],
                problem_statement="Design a 30/60/90-day onboarding program for a new hire.",
                expected_outcome="A documented onboarding plan with clear milestones and success checkpoints.",
                what_should_be_included=(
                    "Week-by-week milestones, stakeholder touchpoints, and how you'd measure onboarding success."
                ),
                why_recruiters_value="Onboarding design signals you think about the full employee lifecycle, not just hiring.",
                resume_bullet_suggestion=(
                    "Designed a 30/60/90-day onboarding program with defined milestones and success metrics."
                ),
                interview_questions=[
                    "How would you measure whether onboarding succeeded?",
                    "What would you do if a new hire was struggling by day 30?",
                    "How would you tailor this for a remote employee?",
                ],
            ),
        ],
    )
)


def _split_tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9+#]+", text.lower()) if t]


# Seniority/qualifier words that don't change *which* role family a title belongs to --
# stripped before matching so "Senior Software Engineer" / "Data Analyst Intern" still
# resolve. Deliberately NOT stripping words like "intelligence" or "systems" that change the
# meaning of the role (e.g. "Business Analyst" vs "Business Systems Analyst" are distinct
# aliases in the catalog on purpose).
_QUALIFIER_WORDS = frozenset(
    {
        "senior",
        "junior",
        "lead",
        "principal",
        "staff",
        "associate",
        "entry",
        "entry-level",
        "intern",
        "internship",
        "trainee",
        "sr",
        "jr",
        "fresher",
        "graduate",
        "new",
        "grad",
    }
)


def _significant_tokens(text: str) -> set:
    return {t for t in _split_tokens(text) if t not in _QUALIFIER_WORDS}


def resolve_role_family(role_name: str) -> Optional[str]:
    """Deterministic alias resolution: role_name matches a family only when its significant
    tokens (qualifier words like "senior"/"intern" stripped) are EXACTLY the same set as one
    of the family's alias/display-name token sets. Exact-set equality (not subset matching)
    is deliberate: an earlier subset-based version incorrectly matched "Business Analyst"
    (tokens {business, analyst}) against Data Analyst's "business intelligence analyst" alias
    (tokens {business, intelligence, analyst}), since {business, analyst} is a subset of that
    longer, unrelated alias. Exact-set matching after qualifier-stripping avoids that whole
    class of false positive; a title that doesn't exactly match a known alias correctly falls
    through to career_path.ai_generation rather than risking a wrong family. Pure function,
    no I/O."""
    if not role_name or not role_name.strip():
        return None
    tokens = _significant_tokens(role_name)
    if not tokens:
        return None
    for family in ROLE_FAMILIES.values():
        candidates = set(family.aliases) | {family.display_name.lower()}
        for alias in candidates:
            if _significant_tokens(alias) == tokens:
                return family.family_key
    return None


def get_role_family(family_key: str) -> Optional[RoleFamilyDef]:
    return ROLE_FAMILIES.get(family_key)
