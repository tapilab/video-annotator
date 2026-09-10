curl -X PUT "$SEARCH_ENDPOINT/indexes/$SEARCH_INDEX?api-version=2024-05-01-preview" \
  -H "Content-Type: application/json" \
  -H "api-key: $SEARCH_ADMIN_KEY" \
  -d '{
    "name": "segments",
    "fields": [
      { "name": "segment_key", "type": "Edm.String", "key": true,  "filterable": true, "sortable": false, "facetable": false },
      { "name": "video_id",     "type": "Edm.String", "searchable": false, "filterable": true, "sortable": true, "facetable": true },
      { "name": "segment_id",   "type": "Edm.String", "searchable": false, "filterable": true, "sortable": true, "facetable": false },

      { "name": "source_url",    "type": "Edm.String", "searchable": false, "filterable": true, "sortable": false, "facetable": false },
      { "name": "source_type",   "type": "Edm.String", "searchable": false, "filterable": true, "sortable": false, "facetable": true },
      { "name": "processed_at",  "type": "Edm.DateTimeOffset", "searchable": false, "filterable": true, "sortable": true, "facetable": false },

      { "name": "start_ms",     "type": "Edm.Int64",  "searchable": false, "filterable": true, "sortable": true, "facetable": false },
      { "name": "end_ms",       "type": "Edm.Int64",  "searchable": false, "filterable": true, "sortable": true, "facetable": false },

      { "name": "text",         "type": "Edm.String", "searchable": true,  "filterable": false, "sortable": false, "facetable": false },

      { "name": "embedding",    "type": "Collection(Edm.Single)", "searchable": true,
        "dimensions": 1536,
        "vectorSearchProfile": "vs_profile"
      },

      { "name": "pred_labels",       "type": "Collection(Edm.String)", "searchable": false, "filterable": true, "sortable": false, "facetable": true },
      { "name": "pred_confidence",   "type": "Edm.Double", "searchable": false, "filterable": true, "sortable": true, "facetable": false },
      { "name": "pred_rationale",    "type": "Edm.String", "searchable": true, "filterable": false, "sortable": false, "facetable": false },
      { "name": "pred_label_details","type": "Edm.String", "searchable": false, "filterable": false, "sortable": false, "facetable": false },
      { "name": "guideline_version", "type": "Edm.String", "searchable": false, "filterable": true, "sortable": true, "facetable": true }
    ],
    "vectorSearch": {
      "algorithms": [
        { "name": "hnsw_algo", "kind": "hnsw" }
      ],
      "profiles": [
        { "name": "vs_profile", "algorithm": "hnsw_algo" }
      ]
    }
  }'
