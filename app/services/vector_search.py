"""Tenant-Isolated Vector Search Module for MediAssist AI.

Provides a tenant-safe wrapper for future vector/semantic search operations.
Currently documents the correct pattern and raises errors if called without
clinic_id to prevent cross-tenant data leakage.

CRITICAL SECURITY RULE:
  When using pgvector or any embedding-based search, the clinic_id (tenant ID)
  MUST be applied as a pre-filter BEFORE the similarity distance computation.

  WRONG (leaks data across tenants):
    SELECT * FROM embeddings ORDER BY embedding <-> query_vec LIMIT 5

  CORRECT (tenant-safe):
    SELECT * FROM embeddings
    WHERE clinic_id = $1
    ORDER BY embedding <-> query_vec LIMIT 5

  The WHERE clause must be an exact equality filter, not an approximation.
  This eliminates cross-tenant embedding neighbors entirely.

Current state:
  The FAQ engine uses keyword matching (no embeddings yet).
  When pgvector is adopted, all search calls MUST go through this module.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TenantIsolationError(Exception):
    """Raised when a vector search is attempted without a clinic_id.
    
    This is a security guard — preventing cross-tenant data leakage.
    """
    pass


class VectorSearchService:
    """Tenant-isolated vector search wrapper.
    
    All search methods enforce clinic_id pre-filtering before
    similarity computation.
    """

    async def search_similar(
        self,
        clinic_id: str,
        query_embedding: list[float],
        table: str,
        embedding_column: str = "embedding",
        limit: int = 5,
        extra_filters: Optional[dict] = None,
    ) -> list[dict]:
        """Search for semantically similar records within a clinic's data.

        Args:
            clinic_id: REQUIRED tenant identifier. Raises TenantIsolationError if empty.
            query_embedding: Float vector from embedding model.
            table: Supabase table name to search.
            embedding_column: Column containing the embedding vectors.
            limit: Maximum results to return.
            extra_filters: Additional key=value filters to apply.

        Returns:
            List of matching record dicts, ordered by similarity.

        Raises:
            TenantIsolationError: If clinic_id is empty or None.
        """
        # SECURITY GUARD: Never search without tenant scope
        if not clinic_id or not clinic_id.strip():
            raise TenantIsolationError(
                "clinic_id is required for all vector search operations. "
                "Searching without a tenant scope risks cross-tenant data leakage."
            )

        from app.database import supabase

        try:
            # Use Supabase RPC to call pgvector similarity function
            # The SQL function MUST include WHERE clinic_id = p_clinic_id
            rpc_params = {
                "p_clinic_id": clinic_id,
                "p_query_embedding": query_embedding,
                "p_table": table,
                "p_limit": limit,
            }

            if extra_filters:
                rpc_params["p_filters"] = extra_filters

            result = supabase.rpc("search_similar_tenant_scoped", rpc_params).execute()
            return result.data or []

        except Exception as e:
            if "does not exist" in str(e).lower():
                # pgvector/function not set up yet — return empty gracefully
                logger.debug(f"Vector search not available yet (pgvector not configured): {e}")
                return []
            logger.error(f"Vector search error: {e}")
            return []

    async def search_faqs(
        self,
        clinic_id: str,
        query_embedding: list[float],
        lang: str = "en",
        limit: int = 3,
    ) -> list[dict]:
        """Search clinic-specific FAQ embeddings.

        This is the primary semantic FAQ lookup. Falls back gracefully to
        keyword matching (handled in faq_engine.py) if pgvector unavailable.

        Args:
            clinic_id: Tenant identifier.
            query_embedding: Embedded query vector.
            lang: Language filter ("en", "hi", "te").
            limit: Max FAQs to return.

        Returns:
            List of FAQ dicts with 'question', 'answer', 'similarity' fields.
        """
        if not clinic_id:
            raise TenantIsolationError("clinic_id required for FAQ vector search")

        return await self.search_similar(
            clinic_id=clinic_id,
            query_embedding=query_embedding,
            table="clinic_faqs",
            limit=limit,
            extra_filters={"lang": lang},
        )

    def validate_tenant_scope(self, clinic_id: Optional[str]) -> None:
        """Raise TenantIsolationError if clinic_id is not provided.
        
        Use this as a guard at the start of any data access function.
        
        Example:
            vector_search.validate_tenant_scope(clinic_id)
            # safe to proceed
        """
        if not clinic_id:
            raise TenantIsolationError(
                "Tenant scope (clinic_id) is required for all data operations."
            )


# Global instance
vector_search = VectorSearchService()
