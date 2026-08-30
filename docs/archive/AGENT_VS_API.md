# Agent vs API: What You Actually Need

## The Key Difference

### **API (what we built first)**
```
User → POST /extract → FastAPI → OpenAI → Return JSON
```
- ❌ **NOT an agent** — just an API endpoint
- ❌ No reasoning, no state, no decision-making
- ❌ Stateless request/response

### **Agent (what you should build)**
```
User query/PDF → Agent receives it → Agent reasons about what to do
    ↓
Agent makes decision → Agent calls tools → Agent observes results
    ↓
Agent adjusts plan if needed → Agent takes next action → Complete
```
- ✅ **IS an agent** — autonomous reasoning system
- ✅ Agent has state, memory, can reason about results
- ✅ Can loop, retry, adjust based on observations
- ✅ Perceive → Decide → Act → Learn

---

## What Makes Something an Agent?

<cite index="74-1">An agentic AI system is one where the model doesn't just respond to prompts. It reasons about goals, plans steps, uses tools, observes results, and adjusts its approach.</cite>

**Your financial extraction should be an agent because:**
1. It receives a PDF
2. It reasons about what type of document it is
3. It decides what fields to extract
4. It uses tools (OpenAI vision API)
5. It observes the results
6. It validates and adjusts if confidence is low

---

## LangGraph: Build Real Agents (Not LangGraph Cloud)

<cite index="75-1">LangGraph is a low-level orchestration framework for building, managing, and deploying long-running, stateful agents.</cite>

**Key point:** You DON'T need LangGraph Cloud. You can:
- ✅ Build agent with LangGraph locally
- ✅ Deploy to Vercel (no extra service)
- ✅ Agent runs serverless in Vercel environment
- ✅ Costs: Just OpenAI API tokens ($30-40/month)

---

## LangGraph Financial Extraction Agent Structure

```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict

# Define agent state
class FinancialExtractionState(TypedDict):
    document_path: str
    extracted_data: dict
    extraction_confidence: float
    validation_passed: bool
    retry_count: int

# Create graph
workflow = StateGraph(FinancialExtractionState)

# Add nodes (steps)
workflow.add_node("load_document", load_pdf_node)
workflow.add_node("extract_financial_data", extract_node)
workflow.add_node("validate_data", validate_node)
workflow.add_node("format_output", format_node)

# Define edges (control flow)
workflow.add_edge(START, "load_document")
workflow.add_edge("load_document", "extract_financial_data")
workflow.add_edge("extract_financial_data", "validate_data")

# Conditional edge: retry if confidence low
workflow.add_conditional_edges(
    "validate_data",
    should_retry,  # Decision function
    {
        "retry": "extract_financial_data",  # Loop back if confidence < 0.8
        "pass": "format_output"  # Move forward if good
    }
)

workflow.add_edge("format_output", END)

# Compile
agent = workflow.compile()

# Run
result = agent.invoke({
    "document_path": "financial_statement.pdf",
    "extraction_confidence": 0,
    "validation_passed": False,
    "retry_count": 0
})
```

---

## Why LangGraph for Financial Extraction?

### With LangGraph:
- ✅ State is explicit (you see exactly what's happening)
- ✅ Can add conditional logic (retry if confidence low)
- ✅ Built-in error handling and retries
- ✅ Easy to debug (see exactly which node failed)
- ✅ Scales to 6 agents easily (add more nodes/edges)

### Without LangGraph (just FastAPI):
- ❌ State is implicit (hidden in function calls)
- ❌ Hard to add conditional logic
- ❌ Manual error handling
- ❌ Harder to reason about what's happening
- ❌ Scaling to 6 agents is messy

---

## LangGraph on Vercel: How It Works

**You don't need LangGraph Cloud.** You deploy the agent itself to Vercel:

```
Vercel (FastAPI server)
    ↓
FastAPI endpoint: POST /extract
    ↓
LangGraph agent runs inside Vercel
    ↓
Agent calls OpenAI API
    ↓
Returns JSON
```

**Cost breakdown:**
- Vercel: $0 (free tier)
- OpenAI API: $30-40/month
- LangGraph Cloud: $0 (not using it)
- **Total: $30-40/month**

---

## Timeline with LangGraph

### Aug 1-2: Setup LangGraph agent locally
- Install `pip install langgraph`
- Define state structure
- Build 4 nodes (load, extract, validate, format)
- Define edges + conditional logic

### Aug 3-5: Test agent locally
- Run agent with test PDFs
- Verify retry logic works
- Check state management

### Aug 6-10: Deploy to Vercel
- Wrap agent in FastAPI endpoint
- Deploy vercel.json
- Test live agent

### Aug 11-15: Client demos
- Ready with real agents (not just APIs)

---

## The Code You'll Need

### 1. `financial_agent.py` (LangGraph agent)
```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict
from financial_extractor import FinancialExtractionAgent

class FinancialExtractionState(TypedDict):
    document_path: str
    document_name: str
    extracted_data: dict
    extraction_confidence: float
    validation_passed: bool
    retry_count: int

# Define nodes
def load_document(state):
    # Load PDF
    return {"document_path": state["document_path"], "retry_count": 0}

def extract_financial_data(state):
    # Call OpenAI to extract
    agent = FinancialExtractionAgent()
    result = agent.extract_from_pdf(state["document_path"])
    return {
        "extracted_data": result,
        "extraction_confidence": result.extraction_confidence
    }

def validate_data(state):
    # Check if extraction is good
    return {"validation_passed": state["extraction_confidence"] > 0.8}

def should_retry(state):
    # Decision logic
    if not state["validation_passed"] and state["retry_count"] < 2:
        return "retry"
    return "pass"

def format_output(state):
    # Format to JSON/CSV/Excel
    from output_formatter import FinancialDataFormatter
    formatter = FinancialDataFormatter()
    return {
        "output": formatter.to_json(state["extracted_data"])
    }

# Build graph
workflow = StateGraph(FinancialExtractionState)
workflow.add_node("load_document", load_document)
workflow.add_node("extract_financial_data", extract_financial_data)
workflow.add_node("validate_data", validate_data)
workflow.add_node("format_output", format_output)

workflow.add_edge(START, "load_document")
workflow.add_edge("load_document", "extract_financial_data")
workflow.add_edge("extract_financial_data", "validate_data")
workflow.add_conditional_edges(
    "validate_data",
    should_retry,
    {"retry": "extract_financial_data", "pass": "format_output"}
)
workflow.add_edge("format_output", END)

# Export compiled agent
agent = workflow.compile()
```

### 2. `api.py` (FastAPI wrapper — same as before, but calls agent)
```python
from fastapi import FastAPI, UploadFile, File
from financial_agent import agent  # Import LangGraph agent

app = FastAPI()

@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    # Save file temporarily
    pdf_path = f"/tmp/{file.filename}"
    with open(pdf_path, "wb") as f:
        f.write(await file.read())
    
    # Run LangGraph agent
    result = agent.invoke({
        "document_path": pdf_path,
        "document_name": file.filename,
        "extracted_data": {},
        "extraction_confidence": 0,
        "validation_passed": False,
        "retry_count": 0
    })
    
    return {"status": "success", "data": result["output"]}
```

---

## Bottom Line

**You were right:**
- ❌ What we built first = API (not an agent)
- ✅ What you need = LangGraph agent deployed on Vercel

**LangGraph vs LangGraph Cloud:**
- ❌ LangGraph Cloud = Hosted service ($100+/month)
- ✅ LangGraph library = Local framework (free) → deploy to Vercel

**Aug 15 Plan:**
1. Build LangGraph agent (Aug 1-5)
2. Deploy to Vercel (Aug 6-10)
3. Live demos (Aug 11-15)

**Cost:**
- $0 LangGraph (open source)
- $0 Vercel
- $30-40 OpenAI API
- **Total: $30-40/month**

---

## Next Action

Should I rebuild the financial extraction with proper LangGraph agent structure?

**Yes → I'll create:**
1. `financial_agent.py` (LangGraph agent with state, nodes, edges)
2. Updated `api.py` (calls the agent, not raw OpenAI)
3. Updated `requirements.txt` (add langgraph)
4. Keep everything else same (still deploy to Vercel, no LangGraph Cloud)

Ready?
