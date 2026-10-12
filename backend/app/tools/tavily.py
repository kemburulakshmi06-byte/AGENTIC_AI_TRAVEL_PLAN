from __future__ import annotations

from typing import Any, Literal

from pydantic import ValidationError

from ..schemas.external import TravelSearchResult, TravelSearchSource
from .base import TravelAPIResponseError, TravelAPITool

_SEARCH_URL = "https://api.tavily.com/search"


class TavilyTool(TravelAPITool):
    provider = "tavily"

    async def search_travel_information(
        self,
        query: str,
        *,
        search_depth: Literal["basic", "advanced"] = "basic",
        max_results: int = 5,
    ) -> TravelSearchResult:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must not be empty.")
        if len(normalized_query) > 400:
            raise ValueError("query must be 400 characters or fewer.")
        if search_depth not in {"basic", "advanced"}:
            raise ValueError("search_depth must be basic or advanced.")
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20.")
        identifier = {
            "query": normalized_query,
            "search_depth": search_depth,
            "max_results": max_results,
        }

        async def load() -> TravelSearchResult:
            payload = await self.request_json(
                "POST",
                _SEARCH_URL,
                json_body={
                    "api_key": self.api_key("TAVILY_API_KEY"),
                    "query": normalized_query,
                    "search_depth": search_depth,
                    "max_results": max_results,
                    "include_answer": False,
                },
            )
            results = payload.get("results")
            if not isinstance(results, list):
                raise TravelAPIResponseError(
                    self.provider, "search response has no results list."
                )
            sources: list[TravelSearchSource] = []
            seen_urls: set[str] = set()
            try:
                for result in results:
                    if not isinstance(result, dict):
                        raise TravelAPIResponseError(
                            self.provider, "search response contained an invalid source."
                        )
                    source = TravelSearchSource(
                            title=result.get("title") or "Untitled source",
                            url=result["url"],
                            summary=result.get("content") or "",
                            content_is_untrusted=True,
                        )
                    if str(source.url) not in seen_urls:
                        sources.append(source)
                        seen_urls.add(str(source.url))
                return TravelSearchResult(query=normalized_query, sources=sources)
            except (KeyError, TypeError, ValidationError) as exc:
                raise TravelAPIResponseError(
                    self.provider, "search response contained an invalid source."
                ) from exc

        result = await self.cached(
            "search",
            identifier,
            lambda sources: TravelSearchResult(
                query=normalized_query,
                sources=[TravelSearchSource.model_validate(source) for source in sources],
            ),
            load,
            serializer=lambda search_result: (
                [source.model_dump(mode="json") for source in search_result.sources]
                if search_result is not None
                else None
            ),
        )
        if result is None:
            raise TravelAPIResponseError(
                self.provider, "provider returned no search result."
            )
        return result
