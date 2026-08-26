output "image_id" {
  description = "Resolved, integrity-checked image ID."
  value       = terraform_data.admission_gate.output.image_id
}

output "region" {
  value = terraform_data.admission_gate.output.region
}

output "version" {
  value = terraform_data.admission_gate.output.version
}

output "channel_generation" {
  value = tonumber(terraform_data.admission_gate.output.generation)
}

output "admission" {
  value = local.admission
}
