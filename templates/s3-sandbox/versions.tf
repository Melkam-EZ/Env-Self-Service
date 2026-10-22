terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

provider "aws" {
  region = var.region

  # Every resource is tagged so the portal (and the orphan scan) can tell who owns what.
  default_tags {
    tags = {
      "managed-by" = "envportal"
      env_id       = var.env_id
      env_name     = var.env_name
      team         = var.team
      owner        = var.owner
      expires_at   = var.expires_at
    }
  }
}
