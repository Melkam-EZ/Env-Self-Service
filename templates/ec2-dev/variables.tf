variable "env_id" { type = string }
variable "env_name" { type = string }
variable "team" { type = string }
variable "owner" { type = string }
variable "expires_at" { type = string }
variable "region" { type = string }

variable "instance_type" {
  type    = string
  default = "t3.micro"
}

variable "volume_gb" {
  type    = number
  default = 10
}
