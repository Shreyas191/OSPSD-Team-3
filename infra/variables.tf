variable "project_id" {
  description = "The team's Google Cloud project that holds every resource below."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run and Artifact Registry. us-central1 has the free tiers DESIGN.md relies on."
  type        = string
  default     = "us-central1"
}

variable "environments" {
  description = "One Cloud Run service per environment: a disposable one for testing and the one we demonstrate."
  type        = set(string)
  default     = ["test", "demo"]
}

variable "initial_image" {
  description = <<-EOT
    Image a new service starts with, before the pipeline has deployed one. After that the
    pipeline deploys each release by digest, and OpenTofu ignores the image (see run.tf).
  EOT
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "max_in_flight_requests" {
  description = "The service's own limit on requests in flight (MAX_IN_FLIGHT_REQUESTS); extra requests get 503 service_busy."
  type        = number
  default     = 10
}

variable "container_concurrency" {
  description = "Requests Cloud Run sends to one instance at once. Above max_in_flight_requests, so excess requests reach our limiter and get the shared 503 body instead of a Cloud Run error."
  type        = number
  default     = 20
}

variable "max_instances" {
  description = "Upper bound on instances per service. Bounds both cost and the rate of calls to Dropbox."
  type        = number
  default     = 2
}

variable "public" {
  description = "Let anyone call the service without a Google identity. The API has no caller authentication of its own; graders and the conformance suite need to reach it."
  type        = bool
  default     = true
}

variable "circleci_org_id" {
  description = "CircleCI organization ID (Organization Settings > Overview). Not a secret; it is in every token CircleCI issues."
  type        = string
}

variable "circleci_project_id" {
  description = "CircleCI project ID of Shreyas191/OSPSD-Team-3 (Project Settings > Overview). Only this project's pipelines may deploy."
  type        = string
}

variable "deploy_branches" {
  description = "Branches whose pipelines may sign in as the deployer. Every other branch, and every fork, gets no Google credentials."
  type        = list(string)
  default     = ["hw2", "main"]
}

variable "alert_email" {
  description = "Who receives the 5xx alert. Kept in terraform.tfvars (git-ignored), not in the repository."
  type        = string
}

variable "alert_5xx_threshold" {
  description = "Server errors (5xx) from one service within 5 minutes above which the alert fires."
  type        = number
  default     = 5
}

variable "composition_environments" {
  description = "Environments that get the secrets for other teams' implementations (composition.tf). Only test runs real providers for HW3; demo stays on the fakes."
  type        = set(string)
  default     = ["test"]
}

variable "vertical_implementations" {
  description = <<-EOT
    Which implementation each environment runs for the issue tracker and the calendar
    (ISSUE_TRACKER_IMPL, CALENDAR_IMPL; see verticals.py). "team4" mounts the Trello
    secrets and "team5" the Google token, so set them only after those secrets have values
    (composition.tf). An environment that is not listed runs "fake" for both.
  EOT
  type = map(object({
    issue_tracker = string
    calendar      = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for impl in values(var.vertical_implementations) :
      contains(["team4", "fake", "failing"], impl.issue_tracker) && contains(["team5", "fake", "failing"], impl.calendar)
    ])
    error_message = "issue_tracker must be team4, fake or failing; calendar must be team5, fake or failing."
  }
}

variable "calendar_token_uploaders" {
  description = "Members (e.g. \"user:someone@example.com\") who may add new versions of the Google token secret but not read it. Kept in terraform.tfvars."
  type        = list(string)
  default     = []
}

variable "agent_model_provider" {
  description = <<-EOT
    MODEL_PROVIDER for each environment's agent service: gemini, scripted or disabled
    (src/course_agent_service). Environments not listed use disabled. Set gemini only after
    the environment's Gemini key is in Secret Manager.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for v in values(var.agent_model_provider) : contains(["gemini", "scripted", "disabled"], v)])
    error_message = "agent_model_provider values must be gemini, scripted or disabled."
  }
}

variable "sign_in_redirect_uris" {
  description = "Environments with Google sign-in on, and where Google sends the browser back (the interface's callback, also listed on the OAuth client). Add an environment only after the google-oauth-client-* secrets have values (sign_in.tf)."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for env in keys(var.sign_in_redirect_uris) : contains(var.environments, env)])
    error_message = "Each key must be one of var.environments."
  }
}

variable "connections" {
  description = "Environments where people connect their own providers (connections.tf), with each provider's return URL; a provider whose URL is empty isn't offered. Add an environment only after token-encryption-key-<env> has a value."
  type = map(object({
    dropbox_redirect_uri  = optional(string, "")
    calendar_redirect_uri = optional(string, "")
    trello_api_key        = optional(string, "")
    trello_return_url     = optional(string, "")
  }))
  default = {}

  validation {
    condition     = alltrue([for env in keys(var.connections) : contains(var.environments, env)])
    error_message = "Each key must be one of var.environments."
  }
}
