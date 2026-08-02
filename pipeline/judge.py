"""Gate 4: the Haiku relevance judgement (spec section 8).

Runs on the top JOBHUNT_DAILY_JUDGE_LIMIT jobs by combined score, never on
everything -- judging all ~800 daily jobs would cost about $4/day against a
budget of roughly $5/month.

**The rationale is the product, not the score.** A number adds nothing the
embedding did not already give us; two sentences explaining why this job fits
is what makes the queue reviewable over a morning coffee.

Thinking is off (classification, not reasoning) and `output_config` is never
sent -- Haiku 4.5 errors on it.
"""

from __future__ import annotations

import json
import logging

from psycopg import AsyncConnection
from pydantic import BaseModel

from pipeline.config import Settings
from pipeline.llm import SpendCapExceeded, call_structured
from pipeline.models import Job
from pipeline.profile import Profile

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
VERDICTS = frozenset({"strong", "possible", "weak"})

# Character floor for the parts of the prefix this module owns -- the task
# instructions, the rubric, and the worked examples. Below Haiku 4.5's
# 4096-token minimum, cache_control is accepted and silently does nothing.
#
# Measured with scripts/measure_prefix.py against the real profile:
#
#     template                       14,244 chars
#     assembled prefix               19,480 chars  ->  4,469 tokens
#     minimum                                          4,096 tokens
#     headroom                                           373 tokens
#
# The ratio is 4.36 chars/token for this content, NOT the ~3.5 a general
# estimate suggests -- four successive guesses all landed short. Re-measure
# rather than extrapolating.
#
# Deliberately guards the template rather than the assembled prefix: the résumé
# is user data of unknown length, so a guard including it would pass or fail
# for reasons the author of an edit does not control. What an edit can break is
# trimming the rubric or the examples, and that is what this catches.
MIN_TEMPLATE_CHARS = 14_000

_INSTRUCTIONS = """You assess how well a job posting fits one specific candidate, \
whose full background is given above.

Give the rationale FIRST, then the verdict, then the score. Reason your way to \
the verdict rather than announcing it and justifying it afterwards, and make sure \
the verdict follows from the rationale you wrote.

The verdict must be exactly "strong", "possible", or "weak"; the score runs from \
0.0 to 1.0; the rationale is at most two sentences.

The rationale is the important part. Write it for the candidate reading their \
morning queue: name the specific overlap or the specific gap. "Matches your \
Python and AWS experience, but wants 8+ years" is useful. "Good fit for your \
skills" is not.

Judge fit, not desirability. A well-matched role at a boring company is a strong \
fit. Do not comment on salary, location, or the company's reputation -- those are \
scored separately."""

# Worked examples. These serve two purposes, and both are load-bearing.
#
# First, calibration: without them Haiku returns "strong" for almost everything,
# because almost everything reaching this gate is a software engineering role
# and the model reads the question as "is this plausible?" rather than "is this
# a better fit than the alternatives?". The weak cases below are the ones that
# do the work.
#
# Second, they carry the static prefix past Haiku 4.5's 4096-token cache
# minimum. Measured: the résumé and bullets alone come to ~1,700 tokens, so
# caching would have been accepted and silently ignored. Above the threshold
# the whole block bills at 0.1x, making the longer prompt about three times
# cheaper per run than the shorter one. Adding real examples was the right way
# to cross that line; padding would not have been.
#
# Keep every example's rationale in the house style: name the specific overlap
# or the specific gap, never "good fit for your skills".
_EXAMPLES = """## Rubric

**strong (0.75-1.00)** -- The core of the role is work the candidate has \
demonstrably done. Primary language and framework overlap, the domain is \
familiar or trivially learnable, and the seniority is at or near their level. \
A role that asks for exactly their stack and one or two unfamiliar tools is \
still strong; competence transfers.

**possible (0.30-0.74)** -- Real overlap with a real gap. Typical shapes: the \
right stack at too senior a level; the right level in an unfamiliar domain; a \
specialist role where the candidate has breadth rather than depth; an adjacent \
discipline that transferable skills could bridge. Use this verdict when a \
motivated application is defensible but not obviously competitive.

**weak (0.00-0.29)** -- A different discipline, or a level mismatch too large \
to argue past. Security engineering, embedded systems, data science, hardware, \
people management, and design are different jobs, not harder versions of this \
one. Shared programming languages do not make them close.

Bias toward the lower verdict when genuinely torn. A false "strong" costs a \
wasted application and erodes trust in the queue; a false "possible" costs \
nothing, because the candidate still sees it and decides.

## Rationale style

Name the specific thing. Every rationale must reference at least one concrete \
element of the candidate's background or one concrete requirement of the \
posting.

Good: "Matches your Docker and AWS EC2 deployment work, but the role centers on \
Kubernetes operators, which is not in your background."
Good: "Your TF-IDF and PageRank implementation is directly relevant to the \
ranking work described."
Bad: "Good fit for your skills and experience."
Bad: "This role aligns well with your background."
Bad: "Strong match for a full-stack engineer."

Never mention salary, location, remote policy, or company prestige. Those are \
scored separately and repeating them here wastes the two sentences.

## Worked examples

These show the expected calibration and rationale style. Match this level of \
specificity.

---
**Posting:** Full Stack Software Engineer, API Experience — build and operate \
customer-facing API surfaces in Python and TypeScript, with React dashboards \
and AWS deployment.

verdict: strong
score: 0.92
rationale: Directly matches your Python/TypeScript full-stack work and your \
LLM API integration experience at CloudBase. The API-surface ownership is the \
same shape as the client portals you shipped.

---
**Posting:** Software Engineer, Agent Productivity — build internal tooling \
around LLM agents; Python, prompt engineering, evaluation harnesses.

verdict: strong
score: 0.88
rationale: Your document-extraction pipeline is exactly this work — dynamic \
prompt assembly, per-document error isolation, retry logic. The evaluation \
angle is new but adjacent.

---
**Posting:** Senior Software Engineer, Data Lake — own petabyte-scale Spark \
and Iceberg pipelines; 8+ years distributed data systems required.

verdict: possible
score: 0.45
rationale: Python and AWS overlap, but this wants 8+ years of dedicated \
distributed-data work and you have three years of general full-stack. The \
scale is well beyond the pipelines you have run.

---
**Posting:** Backend Engineer, Billing and Tax — Ruby on Rails services, \
financial reconciliation, ledger correctness.

verdict: possible
score: 0.40
rationale: Backend service work is in range and your constraint-optimization \
scheduler shows the correctness instinct, but Rails is not in your stack and \
the billing domain is unfamiliar.

---
**Posting:** Senior Security Engineer — threat modeling, detection engineering, \
incident response, SIEM tuning.

verdict: weak
score: 0.12
rationale: Security engineering is a different specialty; nothing in your \
background covers detection, IR, or threat modeling. AWS IAM exposure is not \
close enough to bridge it.

---
**Posting:** Embedded Rust Engineer — firmware for battery management systems, \
no_std, real-time constraints.

verdict: weak
score: 0.05
rationale: No Rust, no embedded, no real-time systems work in your background. \
The Computer Engineering degree is the only connection and it is not enough.

---
**Posting:** Engineering Manager, Platform — lead a team of six, own roadmap \
and headcount, limited hands-on coding.

verdict: weak
score: 0.10
rationale: This is a people-management role and your experience is individual \
contributor. Owning full project lifecycles is not the same as managing \
engineers.

---
**Posting:** Frontend Engineer, Design Systems — React, TypeScript, \
accessibility, component library ownership, close partnership with designers.

verdict: possible
score: 0.55
rationale: React and TypeScript are yours, but this is frontend-specialist \
depth — design systems and accessibility — where your work has been \
full-stack breadth.

---
**Posting:** Solutions Engineer, Enterprise — work with customers to design and \
implement integrations against our API; part technical, part client-facing.

verdict: strong
score: 0.80
rationale: This is your CloudBase role almost exactly — client discovery, \
requirements translation, and integration delivery across 30 engagements. The \
Workato and Zapier work maps straight onto it.

---
**Posting:** Staff Software Engineer, Distributed Systems — set technical \
direction across three teams; 10+ years, deep consensus-protocol experience.

verdict: weak
score: 0.15
rationale: Staff scope with 10+ years required against your three, and \
consensus protocols are not in your background. The gap is a level problem, \
not a stack problem.

---
**Posting:** Machine Learning Engineer — train and fine-tune transformer models, \
own the training pipeline, PyTorch and CUDA.

verdict: weak
score: 0.20
rationale: Your AI work is API integration, RAG, and semantic search, not model \
training. PyTorch, CUDA, and fine-tuning are all absent from your background.

---
**Posting:** DevOps Engineer — Terraform, Kubernetes, CI/CD pipelines, Linux \
server administration, on-call rotation.

verdict: possible
score: 0.60
rationale: Docker, CI/CD, Nginx, PM2, and Linux administration are all yours \
from client deployments, but this is an infrastructure-specialist role and \
your Kubernetes and Terraform exposure is thinner than the work demands.

---
**Posting:** Software Engineer, Internal Tools — Python and React internal \
applications for operations teams; small team, broad ownership.

verdict: strong
score: 0.85
rationale: Python and React with end-to-end ownership is precisely your \
CloudBase pattern, and the resource-allocation scheduler you built for \
operations teams is the same category of tool.

---
**Posting:** Forward Deployed Engineer — embed with enterprise customers, build \
bespoke integrations against our platform, travel occasionally.

verdict: strong
score: 0.86
rationale: Bespoke integration work with direct stakeholder contact is what you \
did across 30 client engagements, and the LLM API integration experience maps \
onto the platform side.

---
**Posting:** Research Engineer — support research scientists by building \
training infrastructure and running experiments; publication track record \
preferred.

verdict: weak
score: 0.18
rationale: Research infrastructure and experiment tracking are outside your \
background, and you have no publications. Your AI work is applied integration, \
not research support.

---
**Posting:** Analytics Engineer — dbt models, Airflow orchestration, warehouse \
design, partner with analysts on metric definitions.

verdict: possible
score: 0.50
rationale: SQL and PostgreSQL are yours and pipeline orchestration is adjacent \
to your extraction work, but dbt, Airflow, and dimensional warehouse modeling \
are all new.

---
**Posting:** Senior iOS Engineer — Swift, SwiftUI, offline-first sync, App \
Store release ownership.

verdict: weak
score: 0.08
rationale: No Swift, no iOS, and no mobile work anywhere in your background. \
Native mobile is a separate discipline from the web stack you have built in.

---
**Posting:** Platform Engineer — build the internal developer platform: CI/CD \
templates, deployment tooling, service scaffolding, developer experience.

verdict: possible
score: 0.65
rationale: Your Docker containerization and CI/CD work created exactly this \
kind of repeatable deployment foundation across client environments, though at \
smaller scale than a dedicated platform team implies.

---
**Posting:** Software Engineer in Test — build automated test frameworks, own \
CI test reliability, partner with product teams on coverage.

verdict: possible
score: 0.45
rationale: You validated your search-engine ranking through unit tests against \
manually derived values, which shows the instinct, but test infrastructure has \
not been your primary work.

---
**Posting:** Technical Program Manager, Infrastructure — drive cross-team \
programs, manage dependencies and timelines, minimal coding.

verdict: weak
score: 0.12
rationale: This is a program-management role and your experience is hands-on \
engineering. Owning project lifecycles as the implementer is not the same as \
coordinating other teams' delivery.

---
**Posting:** Growth Engineer — ship experiments across the signup and \
onboarding funnel; React, TypeScript, A/B testing, fast iteration.

verdict: strong
score: 0.78
rationale: React and TypeScript with rapid end-to-end delivery matches your \
client-facing work directly; A/B experimentation is the one unfamiliar piece \
and it is learnable on the job.

---
**Posting:** Founding Engineer, seed-stage — first engineering hire, build the \
product end to end, no specialisation, expect to own everything.

verdict: possible
score: 0.62
rationale: End-to-end ownership across the full stack is exactly your pattern \
and the breadth suits you, but "founding engineer" usually implies more years \
of judgement about architecture decisions made without support.

---
**Posting:** Integrations Engineer — build and maintain connectors to third-party \
SaaS APIs, own authentication flows and webhook reliability, debug partner \
issues.

verdict: strong
score: 0.90
rationale: This is the centre of your CloudBase work — Workato, Zapier, Make, \
n8n, and Airtable connector delivery across dozens of client systems. \
Webhook and auth debugging is the same territory.

---
**Posting:** Backend Engineer — Go microservices, gRPC, event-driven \
architecture on Kafka, high-throughput systems.

verdict: possible
score: 0.38
rationale: Backend service design transfers and you have the PostgreSQL depth, \
but Go, gRPC, and Kafka are all absent, and high-throughput event systems are \
a step beyond the request/response applications you have built.

---
**Posting:** Developer Advocate — write technical content and sample \
applications, speak at conferences, gather developer feedback for the product \
team.

verdict: possible
score: 0.42
rationale: You can build the sample applications and your client-facing \
requirements work shows the communication side, but public speaking and \
content production are not evidenced anywhere in your background.

---
**Posting:** Data Scientist — statistical modelling, experiment design, causal \
inference, communicate findings to product leadership.

verdict: weak
score: 0.15
rationale: Statistics, experiment design, and causal inference are not in your \
background; your data work is pipelines and retrieval, not analysis. The \
overlap is Python and nothing else.

---
**Posting:** Full Stack Engineer, digital consultancy — deliver client projects \
across varied stacks, scope work with stakeholders, juggle several engagements \
at once.

verdict: strong
score: 0.94
rationale: This is your current role described almost word for word — 30 client \
engagements, requirements gathering through deployment, independently owning \
lifecycles across varied stacks.

---
**Posting:** Software Engineer, Payments — integrate payment providers, handle \
idempotency and reconciliation, PCI compliance awareness.

verdict: possible
score: 0.58
rationale: Third-party API integration with careful failure handling is your \
strength, and your extraction pipeline's per-document error isolation shows the \
idempotency instinct, but payments-specific compliance is unfamiliar ground.

---
**Posting:** Full Stack Engineer, healthtech — patient-facing portals in React \
and Python, HIPAA-compliant data handling, integrations with EHR systems.

verdict: strong
score: 0.82
rationale: React and Python portals with third-party system integration is your \
CloudBase work in a new domain; HIPAA handling is a constraint to learn rather \
than a different skill set.

---
**Posting:** Software Engineer, Trust and Safety — build detection tooling and \
review workflows, work with policy teams on enforcement systems.

verdict: possible
score: 0.48
rationale: The tooling and workflow-system work is within reach given your \
operations platforms, but detection systems and policy enforcement are an \
unfamiliar problem domain.

---
**Posting:** Senior Technical Writer — own API reference documentation, write \
tutorials, maintain the docs site, partner with engineering.

verdict: weak
score: 0.14
rationale: Technical writing is a distinct profession and nothing in your \
background is documentation work. Being able to read the API is not the same as \
being able to document it well.

---
**Posting:** Software Engineer, Search — improve relevance ranking, work on \
query understanding and retrieval quality at scale.

verdict: strong
score: 0.76
rationale: You implemented tokenization, Porter stemming, TF-IDF scoring and \
PageRank from scratch, and your semantic-search and vector-embedding work \
covers the modern retrieval side."""


class Relevance(BaseModel):
    """Field order is load-bearing and must not be "tidied".

    Structured output is generated in declaration order. With the verdict
    declared first the model commits before it has reasoned, and the Sharia
    screen -- which had the same ordering -- returned `excluded` for GitLab
    alongside a reason explaining why GitLab is permitted. Rationale first
    gives the model somewhere to think; thinking is off on this call, so these
    fields are the only place that can happen.
    """

    rationale: str
    verdict: str
    score: float


def build_static_prefix(profile: Profile) -> str:
    """The cacheable block. Must be byte-identical across every call.

    Any per-job content here would invalidate the cache on every request --
    exactly the silent failure spec section 11 warns about. Nothing that varies
    per job may enter this string.
    """
    resume = json.dumps(profile.resume, indent=2, sort_keys=True)
    bullets = "\n".join(f"- {b.get('label')}: {b.get('text')}" for b in profile.competency_bullets)
    return (
        "You are assessing job fit for the following candidate.\n\n"
        f"## Résumé\n\n{resume}\n\n"
        f"## Competency summary\n\n{bullets}\n\n"
        f"## Task\n\n{_INSTRUCTIONS}\n\n"
        f"{_EXAMPLES}"
    )


async def judge_job(
    conn: AsyncConnection, job: Job, profile: Profile, settings: Settings
) -> Relevance | None:
    """One judgement, or None if the cap is reached or the call fails."""
    system = [
        {
            "type": "text",
            "text": build_static_prefix(profile),
            # Measured: see scripts/measure_prefix.py. If the prefix is under
            # Haiku's 4096-token minimum this marker is accepted and does
            # nothing at all -- llm.call_structured warns when that happens.
            "cache_control": {"type": "ephemeral"},
        }
    ]
    user = (
        f"Title: {job.title}\n"
        f"Company: {job.company_name}\n"
        f"Location: {job.location or 'unspecified'} ({job.remote_type})\n\n"
        f"Description:\n{job.description[:6000]}"
    )

    try:
        parsed, _usage = await call_structured(
            conn,
            model=MODEL,
            purpose="judge",
            system=system,
            user=user,
            output_format=Relevance,
            settings=settings,
            max_tokens=400,
        )
    except SpendCapExceeded:
        # A normal end state, not an error: raising would abort the run and
        # lose the jobs already scored this pass.
        log.info("judge: daily cap reached; stopping")
        return None
    except Exception as exc:
        log.warning("judge: call failed for %s: %s", job.title, exc)
        return None

    verdict = parsed.verdict if parsed.verdict in VERDICTS else "possible"
    return Relevance(
        verdict=verdict,
        score=min(1.0, max(0.0, parsed.score)),
        rationale=parsed.rationale.strip(),
    )
