# ---------------------------------------------------------------------------
# Vertex AI — Vector Search (Memory Bank semantic layer)
# ---------------------------------------------------------------------------
# Cost note: Index Endpoints bill while UP (~$0.045/hr for small configs).
# We enable by default because early real testing > deferring surprises.
# Tear down with `terraform destroy` when idle for > a day if cost-sensitive.

# Minimal valid JSONL so contents_delta_uri is accepted on first Index create.
# One zero-vector placeholder (768-d = text-embedding-004). Real facts are
# upserted later via STREAM_UPDATE from the Memory Bank.
resource "google_storage_bucket_object" "vector_bootstrap" {
  count  = var.enable_vector_search ? 1 : 0
  name   = "vector-index/bootstrap/bootstrap.json"
  bucket = google_storage_bucket.signals.name
  content = jsonencode({
    id        = "bootstrap-placeholder"
    embedding = [for _ in range(768) : 0.0]
  })
}

resource "google_vertex_ai_index" "signals" {
  count = var.enable_vector_search ? 1 : 0

  project      = var.project_id
  region       = var.region
  display_name = "level-signals-${var.env}"
  description  = "Embedded facts + signals for Level Memory Bank."
  labels       = local.labels

  metadata {
    contents_delta_uri = "gs://${google_storage_bucket.signals.name}/vector-index/bootstrap"
    config {
      dimensions                  = 768 # text-embedding-004
      approximate_neighbors_count = 150
      distance_measure_type       = "COSINE_DISTANCE"
      feature_norm_type           = "UNIT_L2_NORM"
      algorithm_config {
        tree_ah_config {
          leaf_node_embedding_count    = 1000
          leaf_nodes_to_search_percent = 10
        }
      }
    }
  }

  index_update_method = "STREAM_UPDATE"

  depends_on = [
    google_project_service.apis,
    google_storage_bucket.signals,
    google_storage_bucket_object.vector_bootstrap,
  ]
}

resource "google_vertex_ai_index_endpoint" "signals" {
  count = var.enable_vector_search ? 1 : 0

  project                 = var.project_id
  region                  = var.region
  display_name            = "level-signals-endpoint-${var.env}"
  description             = "Serving endpoint for Level Memory Bank vector search."
  labels                  = local.labels
  public_endpoint_enabled = true

  depends_on = [google_project_service.apis]
}

# Deploy the index onto the endpoint. Takes 20–40 minutes the first time.
resource "google_vertex_ai_index_endpoint_deployed_index" "signals" {
  count = var.enable_vector_search ? 1 : 0

  index_endpoint = google_vertex_ai_index_endpoint.signals[0].id
  index          = google_vertex_ai_index.signals[0].id
  deployed_index_id = "level_signals_deployed"

  display_name = "level-signals-deployed-${var.env}"

  # automatic_resources is cheaper for hackathon volumes and avoids
  # machine-type / shard-size mismatches on dedicated nodes.
  automatic_resources {
    min_replica_count = 1
    max_replica_count = 1
  }

  depends_on = [
    google_vertex_ai_index.signals,
    google_vertex_ai_index_endpoint.signals,
  ]
}

# ---------------------------------------------------------------------------
# Model Armor templates (inbound + outbound)
# ---------------------------------------------------------------------------
# google-beta provider — Model Armor is still under the beta surface in some
# regions. If the resource type is unavailable in your provider version,
# create the templates once via Console and paste the resource names into
# LEVEL_MODEL_ARMOR_TEMPLATE_* env vars.

resource "google_model_armor_template" "inbound" {
  count    = var.enable_model_armor ? 1 : 0
  provider = google-beta

  location     = var.region
  template_id  = "level-inbound"
  project      = var.project_id

  filter_config {
    rai_settings {
      rai_filters {
        filter_type = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }
    sdp_settings {
      basic_config {
        filter_enforcement = "ENABLED"
      }
    }
  }

  template_metadata {
    enforcement_type = "INSPECT_AND_BLOCK"
  }

  depends_on = [google_project_service.apis]
}

resource "google_model_armor_template" "outbound" {
  count    = var.enable_model_armor ? 1 : 0
  provider = google-beta

  location    = var.region
  template_id = "level-outbound"
  project     = var.project_id

  filter_config {
    rai_settings {
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "HIGH"
    }
  }

  template_metadata {
    enforcement_type = "INSPECT_AND_BLOCK"
  }

  depends_on = [google_project_service.apis]
}
