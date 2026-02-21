"""
My Bookish Companion - Multi-Agent Reading Assistant

A conversational AI agent system built with Google's Agent Development Kit (ADK)
that helps users discover books, create personalized reading schedules, and
generate engagement material for their reading journey.

ARCHITECTURE:
- Three specialized LLM agents (Discovery, Scheduling, Engagement)
- One deterministic orchestrator managing the workflow state machine
- Tools for fetching book metadata and creating GitHub issues

WORKFLOW:
1. Discovery: User conversation to identify a book
2. Scheduling: Create a daily reading schedule based on user's time availability
3. Engagement: Generate chapter summaries and reflection questions

"""

import logging
import datetime
import json
import os
import time
import urllib.request
import urllib.parse
from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.tools.tool_context import ToolContext
from google.adk.tools import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

# Configure logging for agent transitions and tool calls
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Global system instructions shared across all LLM agents to maintain consistent scope
GLOBAL_GUARDRAILS = """
SYSTEM NOTICE:
You are a specialized agent for 'My Bookish Companion'. 
Your Scope: Books, Reading Schedules, and Literary Engagement.
If the user asks about topics OUTSIDE this scope (e.g., weather, coding, politics), politely decline and steer the conversation back to reading.
Otherwise, proceed with your specific instructions below.
"""

# ============================================================================
# TOOLS
# ============================================================================

# GitHub MCP Toolset: Connects to the GitHub MCP server to create issues
# for reading schedules and engagement content
# NOTE: Update GITHUB_OWNER and GITHUB_REPO to match your repository
github_mcp_toolset = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={
                "GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
                "GITHUB_OWNER": "mosaique258",  # Change to your GitHub username
                "GITHUB_REPO": "my_bookish_companion"  # Change to your repo name
            }
        )
    )
)

def get_today_and_tomorrow(tool_context: ToolContext) -> str:
    """
    Returns today's and tomorrow's date.
    Provides deterministic date information for scheduling (LLMs often guess dates).
    """
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    return f"Today is {today.strftime('%A, %Y-%m-%d')}. Tomorrow is {tomorrow.strftime('%A, %Y-%m-%d')}."

def get_book_details(tool_context: ToolContext, isbn: str = None, title: str = None, author: str = None) -> str:
    """
    Fetches book metadata (page counts) using the Google Books API.
    Implements retries with exponential backoff for API reliability.
    Tries multiple result items from each query to maximize chances of finding page count.

    Supports optional API key via GOOGLE_BOOKS_API_KEY environment variable.
    API key significantly increases rate limits - get one free at:
    https://console.cloud.google.com/apis/library/books.googleapis.com
    """
    # Get API key from environment (optional but highly recommended)
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY", "")

    queries = []
    if title and author:
        queries.append(f'intitle:"{title}" inauthor:"{author}"')
    if title:
        queries.append(f'intitle:"{title}"')
    if author:
        queries.append(f'inauthor:"{author}"')
    if isbn:
        queries.append(f"isbn:{isbn}")

    logger.info(f"BookDetails tool called with title='{title}', author='{author}', isbn='{isbn}'")
    if api_key:
        logger.info("Using Google Books API key for authentication")
    else:
        logger.warning("No API key found - rate limits may be restrictive. Set GOOGLE_BOOKS_API_KEY in .env file.")

    for q in queries:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                safe_q = urllib.parse.quote(q)
                url = f"https://www.googleapis.com/books/v1/volumes?q={safe_q}"
                if api_key:
                    url += f"&key={api_key}"
                logger.info(f"BookDetails tool querying (Attempt {attempt+1}): {url[:100]}...")

                with urllib.request.urlopen(url, timeout=10) as response:
                    data = json.loads(response.read().decode())
                    if "items" in data:
                        logger.info(f"BookDetails tool received {len(data['items'])} results for query '{q}'")

                        # Try up to the first 3 items to find one with page count
                        for idx, item_data in enumerate(data["items"][:3]):
                            item = item_data.get("volumeInfo", {})
                            res_title = item.get("title", "Unknown")
                            res_authors = ", ".join(item.get("authors", ["Unknown"]))
                            page_count = item.get("pageCount")

                            logger.info(f"BookDetails tool checking result {idx+1}: '{res_title}' by {res_authors} (Pages: {page_count})")

                            if page_count:
                                logger.info(f"✓ BookDetails tool SUCCESS: Found page count for '{res_title}'")
                                return f"Found Book: {res_title} by {res_authors}. Total Pages: {page_count}."
                    else:
                        logger.info(f"BookDetails tool: No items found for query '{q}'")

                    # Successfully got a response, no need to retry this query
                    break

            except Exception as e:
                logger.warning(f"Error in get_book_details (Attempt {attempt+1}) for query '{q}': {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt) # Exponential backoff
                else:
                    logger.error(f"Final failure for query '{q}' after {max_retries} attempts.")

    logger.warning("BookDetails tool exhausted all queries without finding page count")
    return "Could not find book details with page counts. Please ask the user for the page count."

def mark_task_complete(tool_context: ToolContext, task_name: str, summary: str = "") -> str:
    """
    Signals task completion and advances the workflow to the next phase.

    This tool explicitly controls the state machine by updating the current_phase,
    ensuring deterministic handoffs between agents.

    Args:
        task_name: Name of the completed task (e.g., "book_discovery", "schedule_creation", "engagement_generation")
        summary: Optional brief summary of what was accomplished

    Returns:
        Confirmation message indicating the phase transition
    """
    logger.info(f"✓ Task marked complete: {task_name} - {summary}")

    # Record task completion
    tool_context.session.state[f"{task_name}_completed"] = True

    # Determine next phase based on completed task
    current_phase = tool_context.session.state.get("current_phase", "discovery")
    next_phase = current_phase  # Default: no change

    if task_name == 'book_discovery':
        next_phase = 'scheduling'
        transition_msg = "Advancing to Scheduling."
    elif task_name == 'schedule_creation':
        next_phase = 'engagement'
        transition_msg = "Advancing to Engagement."
    elif task_name == 'engagement_generation':
        next_phase = 'complete'
        transition_msg = "Workflow Complete."
    else:
        transition_msg = "Staying in current phase."

    # Update the workflow phase
    tool_context.session.state['current_phase'] = next_phase

    return f"Task '{task_name}' marked as complete. {transition_msg}"

# ============================================================================
# SPECIALIZED AGENTS
# ============================================================================

# Each agent has a specific, focused role in the workflow to improve reliability
# and reduce hallucination by limiting scope.

discovery_agent = LlmAgent(
    model='gemini-2.5-flash-lite',
    name='DiscoveryAgent',
    description="Helps the user find a book and identifies it in the session state.",
    instruction=GLOBAL_GUARDRAILS + """Your job is to help the user choose ONE specific book.
    You are the first point of contact: start by greeting the user warmly!
    Then, be conversational: ask 1-3 follow-up questions to understand their mood or preferences before settling on a recommendation.
    When a specific book is chosen, provide the Title and Author. Provide the ISBN IF it is easily available, but do not let a missing ISBN block the process.

    ⚠️ MANDATORY COMPLETION REQUIREMENT ⚠️
    The MOMENT the user confirms their book choice (e.g., "Let's go with this one", "Yes", "That sounds great"), you MUST IMMEDIATELY call the 'mark_task_complete' tool.

    Tool parameters:
    - task_name='book_discovery' (EXACTLY this string, no other value)
    - summary='Book identified: [Title] by [Author]'

    DO NOT:
    - Forget to call this tool
    - Call it with any other task_name (like 'schedule_creation' or 'engagement_generation')
    - Try to create schedules or engagement material (that's not your job)
    - Continue the conversation after the user confirms - just call mark_task_complete

    If you forget to call this tool, the entire workflow will break.""",
    tools=[mark_task_complete],
    output_key="book_identified",  # Session state key where agent output is stored
)

scheduling_agent = LlmAgent(
    model='gemini-2.5-flash-lite',
    name='SchedulingAgent',
    description="Calculates a reading schedule and saves it to GitHub.",
    instruction=GLOBAL_GUARDRAILS + """Your ONLY job is to create a reading schedule table for the book.

    CRITICAL WORKFLOW - FOLLOW IN THIS EXACT ORDER:

    FIRST TURN (Your very first response when you take over):
    ─────────────────────────────────────────────────────────
    1. Extract the book Title and Author from the conversation history
    2. Call BOTH tools (no text output yet):
       - get_book_details(title="...", author="...")
       - get_today_and_tomorrow()
    3. IMMEDIATELY after tools return (in the SAME response), output text:
       - Do NOT greet the user. The greeting already happened in the discovery phase. Just jump straight into scheduling.
       - Immediately ask: "How many minutes per day can you dedicate to reading?"
       - If get_book_details failed, ask the user for the page count instead

    IMPORTANT: Steps 2 and 3 happen in ONE response. Do NOT wait for the user to say something after the tools finish.

    SECOND TURN (After user provides reading time):
    ─────────────────────────────────────────────────────────
    4. Extract the number of minutes from the user's message
    5. Generate the reading schedule table
    6. Post to GitHub using create_issue
    7. Confirm success to the user
    8. Call mark_task_complete

    DETAILED INSTRUCTIONS:

    For Step 4 (Extracting reading time):
    - Look at the user's most recent message
    - If it contains a number or time (30 minutes, 1 hour, 60, etc.), extract it
    - Convert to minutes if needed (1 hour = 60 minutes)
    - If unclear, ask again: "I didn't quite catch that - how many minutes per day?"
    - Be supportive if they only have a few minutes

    For Step 5 (Generate schedule):
    - Calculate daily page targets: (Total Pages) / (Days needed based on pace)
    - Create Markdown table with columns: Date | Reading Session | Page Start | Page End | Pages
    - Schedule starts TOMORROW (not today)
    - For the Reading session, use simple numerical values: Day 1, Day 2, etc.

    For Step 6 (Post to GitHub):
    - Call create_issue tool with ONLY these four fields (no other fields):
      * owner="mosaique258"  # Your GitHub username
      * repo="my_bookish_companion"  # Your repository name
      * title="Reading Schedule: [Book Title]"
      * body=(your markdown table)
    - DO NOT pass assignees, labels, milestone, or any other fields

    For Step 7 (Completion message):
    - After create_issue succeeds, tell user briefly that schedule is ready. 
    - DO NOT wish them happy reading yet - that comes after engagement content is posted in the next phase.

    For Step 8 (Mark complete):
    - MANDATORY: Call mark_task_complete tool with:
      * task_name='schedule_creation' (EXACTLY this string)
      * summary='Reading schedule created and posted to GitHub'
    - DO NOT SKIP THIS or workflow breaks

    CRITICAL RULES:
    - First response MUST include: tool calls + greeting + question (all in ONE turn)
    - NEVER proceed to schedule generation without user's reading time
    - NEVER call create_issue without a completed table
    - NEVER guess dates - use get_today_and_tomorrow only
    - NEVER use internal knowledge for page counts - call get_book_details
    - If any tool fails, inform user clearly""",
    tools=[get_today_and_tomorrow, get_book_details, github_mcp_toolset, mark_task_complete],
    output_key="reading_schedule",
)

engagement_agent = LlmAgent(
    model='gemini-2.5-flash-lite',
    name='EngagementAgent',
    description="Generates summary and reflection questions for the first chapter.",
    instruction=GLOBAL_GUARDRAILS + """You are an automated content generator. You are NOT a chat assistant.
    TASK:
    - Post engagement material to GitHub AND display it to the user.

    CONTEXT:
    The user has already chosen a book (identified by DiscoveryAgent) and a reading schedule has been created (by SchedulingAgent).
    The book title and author are in the conversation history above. Extract them from the prior conversation.
    You MUST use this information to generate engagement material. Do NOT ask the user for the book title or author.

    INSTRUCTIONS:
    1. EXTRACT: From the conversation history above, find the book Title and Author that were already identified.
    2. GENERATE: Create a Summary, 3 Reflection Questions, and 3 Interesting Facts for the first chapter.
    3. RESPOND: Your response MUST contain:
       a) An indication to the user that you have created some interesting engagement material to help them keep on track. Do NOT include the actual engagement material. 
       b) A tool call to 'create_issue' with the exact same content (for GitHub). Pass ONLY owner, repo, title, and body — no assignees, labels, milestone, or other fields.
    4. WAIT: Once the create_issue tool returns success, you may finish your response naturally: Let the user know that the engagement content has been posted to GitHub and is ready for them to view now or at a later point in time and wish them happy reading!
    5. SIGNAL COMPLETION (MANDATORY): Immediately after the GitHub issue is successfully created, you MUST call the 'mark_task_complete' tool.
       Tool parameters:
       * task_name='engagement_generation' (EXACTLY this string)
       * summary='Engagement content created and posted to GitHub'
       DO NOT SKIP THIS STEP or the workflow will break.
       DO NOT call it with any other task_name like 'book_discovery' or 'schedule_creation'.

    STRICT RULES:
    - YOU MUST CALL THE TOOL. DO NOT JUST OUTPUT TEXT.
    - REPOSITORY: owner="mosaique258", repo="my_bookish_companion". Pass ONLY these two fields plus title and body to create_issue — nothing else.
    - DO NOT GREET. NO "Here is...". NO CHATTING.
    - NEVER ASK THE USER FOR THE BOOK TITLE OR AUTHOR. They are already in the conversation history.
    - DO NOT ask clarifying questions. Generate content based on the book identified above.""",
    tools=[github_mcp_toolset, mark_task_complete],
    output_key="engagement_content",
)

# ============================================================================
# ORCHESTRATOR
# ============================================================================

# Python-based coordinator that uses deterministic rules (not an LLM) to route
# between specialized agents based on the current workflow phase.

class BookishOrchestrator(BaseAgent):
    """
    State-machine orchestrator that manages the three-phase reading companion workflow.

    ARCHITECTURE:

    1. WORKFLOW PHASES:
       The orchestrator routes between three sequential phases:
       - "discovery": User describes preferences, agent recommends a book
       - "scheduling": User provides availability, agent creates a reading schedule
       - "engagement": Agent generates engagement material for the first chapter
       - "complete": All phases finished

    2. STATE RECOVERY:
       If current_phase is missing from session state, the orchestrator scans
       the event history for task completion markers to determine the current phase:
       - "Task 'engagement_generation' marked as complete" → phase = "complete"
       - "Task 'schedule_creation' marked as complete" → phase = "engagement"
       - "Task 'book_discovery' marked as complete" → phase = "scheduling"
       - No completion messages found → phase = "discovery"

       Event history is persistent and reliable, making it ideal for state recovery.

    3. PHASE CONTROL:
       Phase transitions are controlled explicitly by the mark_task_complete tool:
       - Each specialized agent calls mark_task_complete when finished
       - The tool updates current_phase to the next phase
       - The orchestrator routes to the appropriate agent based on current_phase

    4. AUTOMATIC TRANSITIONS:
       The orchestrator loops within a single invocation to enable automatic handoffs:
       - Detects phase changes after each agent execution
       - If phase changed: immediately invokes the next agent
       - If phase unchanged: agent is waiting for user input, yield control
    """
    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state

        # =====================================================================
        # STATE RECOVERY
        # =====================================================================
        # Scan event history to determine current phase if state is missing
        if "current_phase" not in state:
            logger.info("[INIT] 'current_phase' missing. Scanning event history for tool outputs...")

            discovery_done = False
            scheduling_done = False
            engagement_done = False

            # Search event history for task completion markers
            for event in ctx.session.events:
                content_str = str(event.content) if hasattr(event, 'content') else ""

                if "Task 'book_discovery' marked as complete" in content_str:
                    discovery_done = True
                if "Task 'schedule_creation' marked as complete" in content_str:
                    scheduling_done = True
                if "Task 'engagement_generation' marked as complete" in content_str:
                    engagement_done = True

            # Restore phase based on completion history (most recent phase takes precedence)
            if engagement_done:
                state["current_phase"] = "complete"
                logger.info("[RECOVERY] Found engagement completion in history. Restoring phase: complete")
            elif scheduling_done:
                state["current_phase"] = "engagement"
                logger.info("[RECOVERY] Found scheduling completion in history. Restoring phase: engagement")
            elif discovery_done:
                state["current_phase"] = "scheduling"
                logger.info("[RECOVERY] Found discovery completion in history. Restoring phase: scheduling")
            else:
                state["current_phase"] = "discovery"
                logger.info("[RECOVERY] No history found. Defaulting phase: discovery")

        # =====================================================================
        # ORCHESTRATION LOOP
        # =====================================================================
        # Loop enables automatic phase transitions within a single invocation
        max_loops = 5
        loop_count = 0

        while loop_count < max_loops:
            loop_count += 1
            current_phase = state["current_phase"]

            logger.info(f"--- Loop {loop_count} | Current Phase: {current_phase} ---")

            # Route to the appropriate agent based on current phase
            if current_phase == "discovery":
                target_agent = discovery_agent
            elif current_phase == "scheduling":
                target_agent = scheduling_agent
            elif current_phase == "engagement":
                target_agent = engagement_agent
            elif current_phase == "complete":
                logger.info("Workflow is complete.")
                break
            else:
                logger.error(f"Unknown phase '{current_phase}'. Resetting to discovery.")
                state["current_phase"] = "discovery"
                continue

            # Execute the selected agent
            phase_before = current_phase

            logger.info(f"Invoking: {target_agent.name}")
            async for event in target_agent.run_async(ctx):
                yield event

            # Check if the agent advanced the phase
            phase_after = state["current_phase"]

            if phase_after != phase_before:
                logger.info(f"✓ Transition Detected: {phase_before} -> {phase_after}")
                # Continue loop to automatically invoke the next agent
                continue
            else:
                # Agent is waiting for user input, yield control
                logger.info(f"{target_agent.name} is waiting for user input.")
                break

# Initialize the Orchestrator with its team of specialists
root_orchestrator = BookishOrchestrator(
    name='BookishOrchestrator',
    sub_agents=[discovery_agent, scheduling_agent, engagement_agent]
)

# ADK CLI entry point
root_agent = root_orchestrator
