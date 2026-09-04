# Agent Note: Runtime identity and clock context

Status: implemented

## Problem

Chat turns asked "what is today" or "what time is it" without receiving a
consistent clock source. The ReAct prompt already rendered a local timestamp,
but hybrid and chitchat paths had no equivalent context. Identity was also
unstable: a provider model could answer "who are you" with its own product and
vendor name instead of Artoo. Capability questions had the same problem: the
model could enumerate generic LLM abilities rather than describe Artoo's
available sources and tools.

## Decision

All chat generation paths now use a shared runtime context contract:

- Artoo is the product identity. The prompt asks for a concise, warm
  introduction grounded in Artoo's role and forbids volunteering the underlying
  model, provider, or version.
- Capability questions must be answered from the current runtime context and
  available tools, not as a generic chat model; unavailable actions must not be
  promised.
- The server renders an authoritative date/time in a resolved timezone.
- The browser sends the optional `timezone_name` IANA field on chat requests.
  Invalid or missing values fall back to server-local time.
- Agent prompts receive the same timezone-aware context; non-agent prompts use
  the shared runtime block as their base system prompt.

## Alternatives considered

**Detect time questions with keyword rules and return server-generated text.**
Rejected because paraphrases, multiple languages, and mixed time/date intents
make the rule set brittle, and a bypass would bypass the ReAct persona and
answer-formatting behavior.

**Keep the current timestamp only in the ReAct prompt and add a separate
identity instruction there.** Rejected because hybrid and chitchat requests
would still lack both guarantees, leaving provider identity leakage unresolved
on the paths used by quick问答 and闲聊.

## Consequences

The frontend and Open API gain one optional field, `timezone_name`. Existing
clients remain valid because it is optional. Every generation turn adds a small
identity and clock prompt block. Strong provider training data cannot be
guaranteed to obey a prompt in every case, so this is a behavioral contract and
guardrail rather than a cryptographic disclosure control. Invalid client
timezone strings are not echoed into the prompt.

## Testing

`backend/.venv`-based focused backend tests verify timezone-aware runtime
context, invalid-timezone fallback, Artoo's introduction and capability
contract, the Agent prompt contract, and non-agent message construction. The
frontend `npm run build` verifies the request payload and TypeScript contract.
