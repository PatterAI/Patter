# LEO — The Adaptive Learning Engine

*Powered by AI. Personalized for every student. Built for teachers.*

Stanford x DeepMind Hackathon | April 12, 2026

---

## Team

- **Francesco Rosciano** — Founder & Full-Stack Engineer
- *(Add additional team members here if applicable)*

---

## Problem

Education is stuck in broadcast mode. Every student receives the same content at the same pace, regardless of how they learn best.

- **Students** disengage when material is too easy or too hard — 65% report feeling disconnected in traditional classrooms
- **Teachers** spend 50%+ of their time creating content and grading instead of teaching
- **Institutions** lack real-time data to intervene before students fall behind
- **Existing edtech** is static — content libraries that don't adapt to individual learners

The result: disengaged students, burned-out teachers, and preventable learning gaps.

---

## Solution

LEO is an AI-powered adaptive learning platform that transforms how students learn and how teachers teach.

### For Teachers
- **One prompt, full module** — Input any topic and LEO generates a complete learning experience (notes, podcasts, mindmaps, quizzes, videos) in minutes
- **Real-time analytics** — Mastery heatmaps, content effectiveness metrics, grade predictions, and student drill-downs
- **Time back** — Focus on teaching, not content creation and manual assessment

### For Students
- **Adaptive difficulty** — Bayesian mastery engine tracks understanding per subtopic and adjusts questions to the right level
- **Multi-modal learning** — Choose notes, audio, visual, or interactive content based on personal preference
- **Clear progress** — See mastery progression and expected grade predictions in real time

---

## Technology

| Component | Implementation |
|-----------|---------------|
| AI Content Generation | OpenAI GPT models with structured educational prompts (Bloom's taxonomy, Webb's DOK) |
| Adaptive Engine | Bayesian mastery tracking per subtopic with real-time difficulty adjustment |
| Teacher Dashboards | Live heatmap analytics, content effectiveness, grade predictions |
| Frontend | Next.js 15 (App Router), React 18, TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.11+), async SQLAlchemy, Pydantic v2 |
| Infrastructure | PostgreSQL 15, Redis 7, Celery workers, Firebase Auth, AWS S3 |
| Deployment | Terraform-managed AWS (ECS, RDS, ElastiCache, ALB) |

---

## Market Opportunity

- **$400B** global EdTech market by 2028 (HolonIQ)
- **Initial target:** Individual teachers and tutors (bottom-up, product-led adoption)
- **Expansion:** Classroom -> school-wide -> district -> enterprise
- **Competition gap:** No existing platform combines adaptive learning + AI content generation + teacher analytics

---

## Business Model

| Tier | Price | Audience |
|------|-------|----------|
| Free | $0 | Individual teachers (up to 30 students) |
| Pro | $10/teacher/month | Unlimited students, advanced analytics, all content types |
| Enterprise | Custom | Schools and districts (SSO, admin, compliance, support) |

---

## What We Built

- Working adaptive learning engine with Bayesian mastery tracking
- AI content generation pipeline (notes, mindmaps, quizzes, podcasts, video curation)
- Teacher analytics dashboard with mastery heatmaps and grade predictions
- Multi-tenant architecture with organization-level data isolation
- Full infrastructure-as-code deployment (Terraform + Docker)

*(Update after hackathon day with specific prototype demo details)*

---

## Contact

- **Francesco Rosciano**
- *(Add email / LinkedIn / website here)*
