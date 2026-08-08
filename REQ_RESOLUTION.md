# Requirement resolution

A step-by-step trace from [`REQ_SPEC.md`](REQ_SPEC.md) to the code that satisfies it.

For every one of the **53 steps** across **10 phases**: what the specification asked
for, how it was satisfied, where in the tree that lives, and what proves it. The
specification records *what was found* while building; this document records *what
exists now*, so a reader can check the claim rather than trust the narrative.

## How to read this

| Field | Meaning |
|---|---|
| **Required** | What the specification asks for, in its own terms |
| **Satisfied by** | How the requirement is met, and any deviation from the letter of the spec |
| **Where** | The files that carry it |
| **Verified by** | The tests or checks that would fail if it stopped being true |

**Status** is one of:

| | |
|---|---|
| ✅ | Satisfied as specified |
| ⚠️ | Satisfied, with a deviation or limitation recorded in the entry |
| ❌ | Not satisfied |

A deviation is not a failure — several were deliberate decisions taken during the
build, and each says why. What matters is that nothing is claimed to be covered when
it is not.

## Progress

Entries are filled in phase by phase. This table is the index; each phase links to its
section below.

| Phase | Title | Steps | Resolved |
|---|---|---|---|
| [1](#phase-1) | Foundation, sealed networking, and the configuration contract | 4 | _pending_ |
| [2](#phase-2) | Local service layer — Ollama and Neo4j clients | 5 | _pending_ |
| [3](#phase-3) | Document ingestion and knowledge graph construction | 8 | _pending_ |
| [4](#phase-4) | Agent profile generation | 5 | _pending_ |
| [5](#phase-5) | Simulation configuration generation | 4 | _pending_ |
| [6](#phase-6) | Simulation execution engine | 6 | _pending_ |
| [7](#phase-7) | Monitoring, data access, and agent interviews | 5 | _pending_ |
| [8](#phase-8) | Report generation | 4 | _pending_ |
| [9](#phase-9) | Frontend | 7 | _pending_ |
| [10](#phase-10) | Integration testing, egress verification, and operations | 5 | _pending_ |
| | **Total** | **53** | **0 / 53** |

---

## Phase 1

**Foundation, sealed networking, and the configuration contract**

> Specification: [`REQ_SPEC.md` line 78](REQ_SPEC.md#L78)

### 1.1 — Repository scaffold

> Specification: [line 80](REQ_SPEC.md#L80)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 1.2 — The configuration module

> Specification: [line 83](REQ_SPEC.md#L83)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 1.3 — Sealed container networking

> Specification: [line 102](REQ_SPEC.md#L102)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 1.4 — Egress guard test unit

> Specification: [line 127](REQ_SPEC.md#L127)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 2

**Local service layer — Ollama and Neo4j clients**

> Specification: [`REQ_SPEC.md` line 141](REQ_SPEC.md#L141)

### 2.1 — LLM client

> Specification: [line 143](REQ_SPEC.md#L143)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 2.2 — Retry, timeout, and concurrency control

> Specification: [line 158](REQ_SPEC.md#L158)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 2.3 — Embedding service

> Specification: [line 173](REQ_SPEC.md#L173)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 2.4 — Neo4j storage layer

> Specification: [line 190](REQ_SPEC.md#L190)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 2.5 — Service client test units

> Specification: [line 210](REQ_SPEC.md#L210)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 3

**Document ingestion and knowledge graph construction**

> Specification: [`REQ_SPEC.md` line 224](REQ_SPEC.md#L224)

### 3.1 — File parsing

> Specification: [line 226](REQ_SPEC.md#L226)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 3.2 — Chunking

> Specification: [line 243](REQ_SPEC.md#L243)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 3.3 — Ontology generation

> Specification: [line 258](REQ_SPEC.md#L258)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 3.4 — Entity and relationship extraction

> Specification: [line 271](REQ_SPEC.md#L271)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 3.5 — Graph construction

> Specification: [line 300](REQ_SPEC.md#L300)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 3.6 — Graph query and search

> Specification: [line 324](REQ_SPEC.md#L324)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 3.7 — Graph API

> Specification: [line 342](REQ_SPEC.md#L342)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 3.8 — Ingestion test units

> Specification: [line 359](REQ_SPEC.md#L359)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 4

**Agent profile generation**

> Specification: [`REQ_SPEC.md` line 377](REQ_SPEC.md#L377)

### 4.1 — Entity-to-persona mapping

> Specification: [line 379](REQ_SPEC.md#L379)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 4.2 — Population expansion

> Specification: [line 396](REQ_SPEC.md#L396)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 4.3 — OASIS profile schema conformance

> Specification: [line 413](REQ_SPEC.md#L413)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 4.4 — Parallel generation with progress

> Specification: [line 434](REQ_SPEC.md#L434)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 4.5 — Profile test units

> Specification: [line 449](REQ_SPEC.md#L449)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 5

**Simulation configuration generation**

> Specification: [`REQ_SPEC.md` line 465](REQ_SPEC.md#L465)

### 5.1 — Scenario derivation

> Specification: [line 467](REQ_SPEC.md#L467)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 5.2 — Action space configuration

> Specification: [line 485](REQ_SPEC.md#L485)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 5.3 — Config persistence and override

> Specification: [line 502](REQ_SPEC.md#L502)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 5.4 — Config test units

> Specification: [line 519](REQ_SPEC.md#L519)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 6

**Simulation execution engine**

> Specification: [`REQ_SPEC.md` line 537](REQ_SPEC.md#L537)

### 6.1 — OASIS integration with local inference

> Specification: [line 539](REQ_SPEC.md#L539)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.2 — Process isolation and IPC

> Specification: [line 576](REQ_SPEC.md#L576)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.3 — Round loop and persistence

> Specification: [line 607](REQ_SPEC.md#L607)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.4 — Graph memory feedback (optional, flagged)

> Specification: [line 624](REQ_SPEC.md#L624)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.5 — Simulation control API

> Specification: [line 641](REQ_SPEC.md#L641)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.6 — Engine test units

> Specification: [line 660](REQ_SPEC.md#L660)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 7

**Monitoring, data access, and agent interviews**

> Specification: [`REQ_SPEC.md` line 680](REQ_SPEC.md#L680)

### 7.1 — Run status and timeline endpoints

> Specification: [line 682](REQ_SPEC.md#L682)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 7.2 — Content access endpoints

> Specification: [line 697](REQ_SPEC.md#L697)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 7.3 — Agent interview

> Specification: [line 712](REQ_SPEC.md#L712)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 7.4 — Environment health

> Specification: [line 732](REQ_SPEC.md#L732)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 7.5 — Monitoring test units

> Specification: [line 743](REQ_SPEC.md#L743)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 8

**Report generation**

> Specification: [`REQ_SPEC.md` line 760](REQ_SPEC.md#L760)

### 8.1 — Report agent

> Specification: [line 762](REQ_SPEC.md#L762)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 8.2 — Grounding and citation

> Specification: [line 781](REQ_SPEC.md#L781)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 8.3 — Report API and persistence

> Specification: [line 796](REQ_SPEC.md#L796)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 8.4 — Report test units

> Specification: [line 815](REQ_SPEC.md#L815)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 9

**Frontend**

> Specification: [`REQ_SPEC.md` line 835](REQ_SPEC.md#L835)

### 9.1 — Application shell

> Specification: [line 837](REQ_SPEC.md#L837)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.2 — Stage 1 — graph build

> Specification: [line 882](REQ_SPEC.md#L882)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.3 — Stage 2 — environment setup

> Specification: [line 899](REQ_SPEC.md#L899)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.4 — Stage 3 — simulation

> Specification: [line 916](REQ_SPEC.md#L916)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.5 — Stage 4 — report

> Specification: [line 941](REQ_SPEC.md#L941)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.6 — Stage 5 — interaction

> Specification: [line 954](REQ_SPEC.md#L954)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.7 — Frontend test units

> Specification: [line 997](REQ_SPEC.md#L997)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 10

**Integration testing, egress verification, and operations**

> Specification: [`REQ_SPEC.md` line 1018](REQ_SPEC.md#L1018)

### 10.1 — Full pipeline integration test

> Specification: [line 1020](REQ_SPEC.md#L1020)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 10.2 — Egress verification suite

> Specification: [line 1037](REQ_SPEC.md#L1037)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 10.3 — Performance baseline

> Specification: [line 1056](REQ_SPEC.md#L1056)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 10.4 — Operational tooling

> Specification: [line 1071](REQ_SPEC.md#L1071)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 10.5 — Documentation

> Specification: [line 1084](REQ_SPEC.md#L1084)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---
