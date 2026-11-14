"""PubMed through NCBI's official API (E-utilities): exact titles, dates and abstracts,
newest first. Read-only, so Jugnu uses it without asking. No key needed at
Jugnu's pace (NCBI allows 3 requests a second without one).
"""
import asyncio
import json
import urllib.parse
import urllib.request

from claude_agent_sdk import create_sdk_mcp_server, tool

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def _get(endpoint, **params):
    params.update(db="pubmed", tool="jugnu")
    url = EUTILS + endpoint + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def search(query, count=10):
    """Newest papers matching a PubMed query, one line each."""
    ids = json.loads(_get("esearch.fcgi", term=query, sort="pub_date",
                          retmax=count, retmode="json"))["esearchresult"]
    if not ids["idlist"]:
        return f"No PubMed results for {query!r}."
    info = json.loads(_get("esummary.fcgi", id=",".join(ids["idlist"]), retmode="json"))["result"]
    lines = [f"{ids['count']} results for {query!r}; newest {len(ids['idlist'])}:"]
    for pmid in ids["idlist"]:
        p = info[pmid]
        first = p["authors"][0]["name"] if p.get("authors") else "no author listed"
        lines.append(f"- PMID {pmid} | {p.get('pubdate', '')} | {p.get('source', '')} | "
                     f"{first} et al. | {p.get('title', '').strip()}")
    return "\n".join(lines)


def abstract(pmid):
    return _get("efetch.fcgi", id=pmid, rettype="abstract", retmode="text").strip()


def _text(fn, *args):
    async def run():
        try:
            return {"content": [{"type": "text", "text": await asyncio.to_thread(fn, *args)}]}
        except Exception as e:  # tell Claude, don't crash the turn
            return {"content": [{"type": "text", "text": f"PubMed failed: {e}"}], "is_error": True}
    return run()


@tool("search_pubmed", "Search PubMed for papers, newest first. Use this, not web search, "
      "for any question about the literature: latest papers, who published what, papers on "
      "a gene or protein. Takes a PubMed query (e.g. 'sleep AND memory', 'aspirin[Title] AND "
      "2026[dp]'). Returns PMID, date, journal, first author and title per paper.",
      {"query": str, "count": int})
async def search_pubmed(args):
    return await _text(search, args["query"], min(int(args.get("count") or 10), 30))


@tool("read_abstract", "The abstract of one PubMed paper, by PMID. Read it before "
      "describing what a paper found.", {"pmid": str})
async def read_abstract(args):
    return await _text(abstract, args["pmid"])


SERVER = create_sdk_mcp_server("pubmed", tools=[search_pubmed, read_abstract])
TOOL_NAMES = ["mcp__pubmed__search_pubmed", "mcp__pubmed__read_abstract"]


if __name__ == "__main__":
    out = search("sleep AND memory", 3)
    print(out)
    assert out.count("PMID") == 3
    pmid = out.splitlines()[1].split()[2]
    print(abstract(pmid)[:400])
    print("ok")
