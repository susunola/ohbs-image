locals {
  policy_args      = var.policy_bundle == "" ? [] : ["--policy", var.policy_bundle]
  environment_args = var.environment == "" ? [] : ["--environment", var.environment]
  state_args       = var.state_dir == "" ? [] : ["--state-dir", var.state_dir]
}

data "external" "ohbs_image" {
  program = concat(
    ["ohbs-image"],
    local.state_args,
    ["consumer", "resolve", var.bucket, var.channel],
    local.policy_args,
    local.environment_args,
    ["--output", "terraform"]
  )
}

locals {
  admission = jsondecode(data.external.ohbs_image.result.admission_json)
}

resource "terraform_data" "admission_gate" {
  input = data.external.ohbs_image.result

  lifecycle {
    precondition {
      condition     = data.external.ohbs_image.result.allowed == "true"
      error_message = "ohbs-image policy denied the selected golden image."
    }
  }
}
