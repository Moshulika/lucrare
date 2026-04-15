# TODO Thesis Expansion

## Keep Existing Content Task

- Add Gemma model details where needed.
	Example anchored in paper: extend the discussion around the open-source model option already mentioned in the model comparison and keep it consistent with the hardware table entry for Gemma 4 from the implementation chapter.

## High-Value Additions

- Add a dedicated architecture chapter for the proposed assistant.
	Example anchored in paper: expand the current LangGraph choice discussion into a full system view by explaining the components behind the claim about "buclele de scriere de cod, feedback, investigare si imbunatatire" and show how model selection, tools, memory, MCP servers, and the UI interact.

- Expand the implementation chapter toward core internal logic, not only setup.
	Example anchored in paper: after the current sections on environment, structure, and configuration, add subsections for prompt flow, command parsing, conversation persistence, model routing, tool invocation, and error handling, using the existing list of commands and configurable options as starting points.

- Add an evaluation or experimental validation chapter.
	Example anchored in paper: define several scenarios based on the functionality you already list, such as selecting a model, using MCP servers, resuming a conversation, or invoking a skill, then discuss whether the assistant completes the task correctly, how long it takes, and what the token cost is.

- Add a conclusions and future work chapter.
	Example anchored in paper: connect the conclusion to the goals from the introduction and explicitly revisit the two out-of-scope items you already named, namely multimodal input and productization.

## Sections That Can Be Deepened

- Turn the current limitations section into a later discussion grounded in your own implementation.
	Example anchored in paper: revisit the context-window, verbosity, and looping issues from the "Puncte slabe ale arhitecturilor agentice" section and explain whether your own assistant shows these behaviors during real usage.

- Add a clearer methodology or requirements section.
	Example anchored in paper: formalize the needs already listed in the "Necesitati pentru dezvoltarea unei solutii agentice" section into functional requirements such as portability, extensibility, multi-model support, and safety, plus non-functional ones like usability and observability.

- Develop the architecture-selection discussion into explicit decision criteria.
	Example anchored in paper: for the framework comparison table, add a short analysis per criterion explaining why state management, branching workflows, and observability mattered more than ease of learning in your final LangGraph decision.

- Expand the model-selection discussion with explicit routing logic.
	Example anchored in paper: build on the section discussing cost, SWE-Bench performance, and model families by explaining how a real assistant should choose between stronger and cheaper models depending on task complexity.

- Expand the security and safety dimension.
	Example anchored in paper: take the sentence about preventing dangerous terminal commands or exposure of sensitive data and turn it into a concrete subsection about guardrails, permission boundaries, and safe tool usage.

## Places Where Existing Material Can Carry More Pages

- Add more analysis after figures and tables instead of leaving them mostly descriptive.
	Example anchored in paper: after the model comparison table and the SWE-Bench figures, explain what conclusions you draw for your own design, not only what the charts show in general.

- Expand the UI chapter into a design case study.
	Example anchored in paper: use the two Textual screenshots and the two terminal-based screenshots to compare the two interface approaches using concrete criteria such as scroll behavior, latency, memory growth, copy-paste usability, and implementation complexity.

- Add a subsection dedicated to the project structure and module responsibilities.
	Example anchored in paper: take the current directory tree figure and explain what responsibility belongs to src, ui, docs, scripts, and configuration files, and why that separation helps extensibility.

- Expand the configuration section with operational scenarios.
	Example anchored in paper: start from the existing description of config.yml and discuss what happens when no API keys are present, when multiple providers are configured, or when the user changes the reasoning effort from the interface.

- Expand the command list into usage scenarios.
	Example anchored in paper: for commands like /model, /mcp, /skill, /plan, and /resume, add short scenario-based explanations that show why each one exists and how it supports the overall architecture.

## Strong Academic Additions

- Add a reproducibility subsection.
	Example anchored in paper: use the existing environment, hardware, software, and Makefile references to explain how another student could set up the project and reproduce the same behavior.

- Add a subsection on observability and debugging.
	Example anchored in paper: build on the mention of LangSmith and LangFuse and explain what traces, metrics, or debugging information are useful when an agent fails or loops.

- Add a subsection on conversation memory and context management.
	Example anchored in paper: connect this to your already stated need for managing conversation, context, and memory, and explain how summaries, resumed chats, or trimmed context help overcome window limits.

- Add a subsection on failure cases.
	Example anchored in paper: use the same assistant capabilities already presented in the UI chapter and describe representative cases where the assistant gives too much output, chooses the wrong tool, or gets stuck.

- Add a subsection comparing your solution with existing assistants.
	Example anchored in paper: build on the earlier references to Codex, Gemini CLI, Claude Code, and open-source alternatives, then explain what your project does similarly and what it intentionally leaves out.