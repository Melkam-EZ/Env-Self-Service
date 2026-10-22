variable "env_id" { type = string }
variable "env_name" { type = string }
variable "team" { type = string }
variable "owner" { type = string }
variable "expires_at" { type = string }
variable "region" { type = string }

variable "message" {
  type    = string
  default = "hello from envp"
}

variable "memory_mb" {
  type    = number
  default = 128
}
