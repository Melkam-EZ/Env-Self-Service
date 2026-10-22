output "file" {
  value = terraform_data.env_file.input.path
}

output "message" {
  value = terraform_data.env_file.input.body
}
