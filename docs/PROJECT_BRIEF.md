# Project Brief

## Working name

**DeepSeek Qwen Gateway**

## Problem

`deepseek4free` offers an unofficial path to DeepSeek Web. Qwen Code is a coding agent that can use OpenAI-compatible model providers. The missing layer is a stable gateway that converts Qwen Code's OpenAI Chat Completions traffic—including tool calls—into DeepSeek Web interactions.

## Goal

Expose a local provider root such as:

```text
http://127.0.0.1:8000/v1
```

Qwen Code should be able to select a `deepseek-web` model backed by this gateway and use it for actual agentic coding tasks.

## Core flow

```text
Qwen Code messages + tools
→ gateway compiles backend prompt
→ DeepSeek chooses tool
→ gateway emits structured OpenAI tool_calls
→ Qwen Code executes tool
→ Qwen Code sends role=tool result
→ gateway continues
→ final answer
```

## Initial user

- one local/personal technical user;
- manually supplied valid DeepSeek Web credential;
- gateway on localhost;
- Qwen Code as the primary client.

## Non-goals for core

- replacing Qwen Code;
- server-side arbitrary coding tool execution;
- multi-tenant SaaS;
- billing/RBAC;
- UI before core agent behavior;
- perfect OpenAI API coverage;
- Responses API;
- parallel tool calls;
- guarantees about DeepSeek private API stability.

## Initial stack

- Python 3.12+
- FastAPI
- Pydantic v2
- SQLite
- pytest
- OpenAI `/v1/chat/completions`
- streamed responses
- single DeepSeek account

## Primary acceptance

A real Qwen Code session must be able to fix a deterministic bug and run tests through **Qwen Code's own tools**, with the gateway acting only as the model/provider translation layer.
