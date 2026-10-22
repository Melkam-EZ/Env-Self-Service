# One resource, two provisioners: create writes the file, destroy removes it.
# Values travel through environment variables, never through the shell command text.
resource "terraform_data" "env_file" {
  input = {
    path = "${var.base_dir}/${var.env_id}.txt"
    body = "${var.greeting} from ${var.env_name} (team ${var.team}, tier ${var.tier})\nowner: ${var.owner}\nexpires: ${var.expires_at}"
  }

  provisioner "local-exec" {
    command = "mkdir -p \"$(dirname \"$TARGET\")\" && printf '%s\\n' \"$BODY\" > \"$TARGET\""
    environment = {
      TARGET = self.input.path
      BODY   = self.input.body
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "rm -f \"$TARGET\""
    environment = {
      TARGET = self.input.path
    }
  }
}
