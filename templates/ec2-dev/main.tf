locals {
  name = "envp-${var.team}-${var.env_name}-${substr(var.env_id, 4, 8)}"
  tags = {
    "managed-by" = "envportal"
    env_id       = var.env_id
    team         = var.team
    owner        = var.owner
  }
}

data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# No ingress rules at all: nothing can connect in. Access is through SSM.
resource "aws_security_group" "box" {
  name        = local.name
  description = "envp dev box: egress only"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "box" {
  name               = local.name
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.box.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "box" {
  name = local.name
  role = aws_iam_role.box.name
}

resource "aws_instance" "box" {
  ami                         = data.aws_ssm_parameter.ami.value
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids      = [aws_security_group.box.id]
  iam_instance_profile        = aws_iam_instance_profile.box.name
  associate_public_ip_address = true # outbound only; lets the SSM agent reach AWS without a NAT gateway
  volume_tags                 = local.tags

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.volume_gb
    encrypted   = true
  }

  tags = {
    Name = local.name
  }
}
