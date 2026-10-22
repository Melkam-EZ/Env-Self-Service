variable "env_id" { type = string }
variable "env_name" { type = string }
variable "team" { type = string }
variable "owner" { type = string }
variable "expires_at" { type = string }

variable "greeting" {
  type    = string
  default = "hello"
}

variable "tier" {
  type    = string
  default = "small"
}

variable "base_dir" {
  type        = string
  description = "Where the demo files are written."
  default     = "/tmp/envportal-demo"
}
