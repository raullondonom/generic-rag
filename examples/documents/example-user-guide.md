# RAG assistant user guide

> Sample document included in the repository to try the assistant right away.
> Ask, for example: "How do I add new documents?" or "Who is responsible for the assistant?".

## What is it?
The RAG assistant answers questions using only the documents in the `documents/` folder.

## How do I add documents?
1. Copy the files into the `documents/` folder (subfolders are supported).
2. Wait up to 60 seconds: the system detects new or modified files and indexes them.
3. An administrator can also press **Update folder** in the administration panel (`/admin`).

## How do I change the AI model?
The administrator chooses it in the administration panel (`/admin`), and it applies to every answer:
- **Claude (Anthropic)**: remote model; requires an Anthropic API key.
- **Ollama (local)**: a model running on your own machine (GPU or CPU).

Previous conversations are kept: open the side panel to pick up any of them where you left off.

## Support
Owner of the assistant: Platform team. Support hours: Monday to Friday, 8:00 to 17:00.
Issues are reported by emailing the help desk with the subject "RAG".
