# Skills and workflows

ARIA has two file-based extension mechanisms that deliberately solve different problems:

- **Skills** change model behavior, personality, procedures, or reusable instructions.
- **Workflows** schedule executable actions at defined times.

## Skills

Skills live under the configured `skills/` directory and are Markdown files.

A skill can contain optional YAML front matter:

```markdown
---
name: Deep Research
priority: 10
---

When performing research, prefer primary sources and verify conflicting claims.
```

`name` controls the display name. If omitted, the file name is used. `priority` controls loading order; higher priority values are loaded earlier in the system prompt.

Everything after the front matter is the skill body and is treated as behavioral instruction text.

## Skill lifecycle

ARIA re-reads skills every turn. Therefore:

1. create/edit a `.md` file;
2. save it;
3. send the next message to ARIA;
4. the new skill content is considered for that turn.

A restart is not normally required.

Use:

```text
/skills
```

to inspect discovered skills.

## Good skill design

A useful skill should be specific about behavior rather than repeating generic assistant instructions.

Good examples:

- research verification procedure;
- coding conventions for a repository;
- response style for a particular project;
- a recurring multi-step workflow;
- domain-specific terminology and constraints.

Avoid putting secrets, API keys, tokens, or machine-specific credentials into skills. Skills become model context.

## Skill priority

If several skills influence the same topic, priority controls their relative prompt ordering. Do not assume a higher priority automatically overrides every other instruction: the final model behavior still depends on the provider, system prompt, user request, and the content of the instructions.

## Workflows

Workflows live under `workflows/` and use YAML. They are loaded by the in-process scheduler.

A minimal workflow:

```yaml
workflows:
  - name: weekday_followup
    cron: "0 17 * * 1-5"
    category: analysis
    action:
      type: deadline_check
    misfire_policy: skip
    enabled: true
```

The cron format has five fields:

```text
minute hour day-of-month month weekday
```

See [`workflows/README.md`](../../workflows/README.md) for the complete supported forms and built-in actions.

## Workflow security

A workflow file existing on disk does not automatically grant it every capability. The action category is checked against scheduler/autonomy configuration.

Read-only analysis requires scheduler analysis to be enabled. Consequential categories require autonomy and the relevant allowlist entries.

LifeOS writes have a second operation-level allowlist.

This allows a repository of workflows to contain potentially useful definitions without giving every workflow unrestricted authority.

## Skills vs workflows

Use a **skill** when the requirement sounds like:

> "When ARIA performs X, it should think/work/respond according to these rules."

Use a **workflow** when the requirement sounds like:

> "At time X, ARIA should execute this defined action."

A workflow can cause ARIA to perform an analysis that is influenced by loaded skills, but the scheduling mechanism and the behavioral-instruction mechanism remain separate.
