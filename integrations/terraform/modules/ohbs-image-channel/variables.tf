variable "bucket" {
  description = "ohbs-image Artifact Registry bucket (normally the OS profile)."
  type        = string
}

variable "channel" {
  description = "Channel to resolve, for example stable or production."
  type        = string
}

variable "policy_bundle" {
  description = "Optional path to an ohbs-image policy bundle."
  type        = string
  default     = ""
}

variable "environment" {
  description = "Policy environment; defaults to the channel name."
  type        = string
  default     = ""
}

variable "state_dir" {
  description = "Optional OHBS_IMAGE_STATE_DIR containing the synchronized registry."
  type        = string
  default     = ""
}
