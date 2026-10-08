# Opt-in public browser entry point. Existing EC2, EIP and databases stay in place.
variable "public_web_enabled" {
  type        = bool
  default     = false
  description = "Create the billable provider-generated HTTPS URL and private ingress."
}

resource "aws_subnet" "web" {
  count             = var.public_web_enabled ? 2 : 0
  vpc_id            = aws_vpc.orion.id
  cidr_block        = "10.76.${count.index + 2}.0/24"
  availability_zone = "ap-south-1${count.index == 0 ? "a" : "b"}"
  tags              = { Name = "orion-web-private-${count.index}" }
}
resource "aws_route_table" "web" {
  count  = var.public_web_enabled ? 1 : 0
  vpc_id = aws_vpc.orion.id
  # Local VPC traffic only; no internet or NAT route.
}
resource "aws_route_table_association" "web" {
  count          = var.public_web_enabled ? 2 : 0
  subnet_id      = aws_subnet.web[count.index].id
  route_table_id = aws_route_table.web[0].id
}
resource "aws_security_group" "web_link" {
  count       = var.public_web_enabled ? 1 : 0
  name_prefix = "orion-web-link-"
  vpc_id      = aws_vpc.orion.id
  description = "API Gateway VPC link; no inbound connections"
}
resource "aws_security_group" "web_alb" {
  count       = var.public_web_enabled ? 1 : 0
  name_prefix = "orion-web-alb-"
  vpc_id      = aws_vpc.orion.id
  description = "Internal ALB; API Gateway VPC link only"
}
resource "aws_security_group" "web_target" {
  count       = var.public_web_enabled ? 1 : 0
  name_prefix = "orion-web-target-"
  vpc_id      = aws_vpc.orion.id
  description = "Private reverse proxy; internal ALB only"
}
resource "aws_security_group_rule" "link_to_alb" {
  count                    = var.public_web_enabled ? 1 : 0
  type                     = "egress"
  security_group_id        = aws_security_group.web_link[0].id
  source_security_group_id = aws_security_group.web_alb[0].id
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
}
resource "aws_security_group_rule" "alb_from_link" {
  count                    = var.public_web_enabled ? 1 : 0
  type                     = "ingress"
  security_group_id        = aws_security_group.web_alb[0].id
  source_security_group_id = aws_security_group.web_link[0].id
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
}
resource "aws_security_group_rule" "alb_to_target" {
  count                    = var.public_web_enabled ? 1 : 0
  type                     = "egress"
  security_group_id        = aws_security_group.web_alb[0].id
  source_security_group_id = aws_security_group.web_target[0].id
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
}
resource "aws_security_group_rule" "target_from_alb" {
  count                    = var.public_web_enabled ? 1 : 0
  type                     = "ingress"
  security_group_id        = aws_security_group.web_target[0].id
  source_security_group_id = aws_security_group.web_alb[0].id
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
}
resource "aws_lb" "web" {
  count                      = var.public_web_enabled ? 1 : 0
  name                       = "orion-web-private"
  internal                   = true
  load_balancer_type         = "application"
  subnets                    = aws_subnet.web[*].id
  security_groups            = [aws_security_group.web_alb[0].id]
  drop_invalid_header_fields = true
  desync_mitigation_mode     = "strictest"
  depends_on                 = [aws_route_table_association.web]
}
resource "aws_lb_target_group" "web" {
  count    = var.public_web_enabled ? 1 : 0
  name     = "orion-web-portal"
  port     = 8080
  protocol = "HTTP"
  vpc_id   = aws_vpc.orion.id
  health_check {
    path    = "/_orion_health"
    matcher = "200"
  }
}
resource "aws_lb_target_group_attachment" "web" {
  count            = var.public_web_enabled ? 1 : 0
  target_group_arn = aws_lb_target_group.web[0].arn
  target_id        = aws_instance.orion.id
  port             = 8080
}
resource "aws_lb_listener" "web" {
  count             = var.public_web_enabled ? 1 : 0
  load_balancer_arn = aws_lb.web[0].arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web[0].arn
  }
}
resource "aws_apigatewayv2_vpc_link" "web" {
  count              = var.public_web_enabled ? 1 : 0
  name               = "orion-web-private"
  security_group_ids = [aws_security_group.web_link[0].id]
  subnet_ids         = aws_subnet.web[*].id
}
resource "aws_apigatewayv2_api" "web" {
  count         = var.public_web_enabled ? 1 : 0
  name          = "orion-paper-web"
  protocol_type = "HTTP"
  # Same origin for browser UI and API; deliberately no permissive CORS policy.
}
resource "aws_apigatewayv2_integration" "web" {
  count                  = var.public_web_enabled ? 1 : 0
  api_id                 = aws_apigatewayv2_api.web[0].id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  integration_uri        = aws_lb_listener.web[0].arn
  connection_type        = "VPC_LINK"
  connection_id          = aws_apigatewayv2_vpc_link.web[0].id
  payload_format_version = "1.0"
  timeout_milliseconds   = 30000
  request_parameters = {
    "overwrite:path"                     = "$request.path"
    "overwrite:header.x-orion-viewer-ip" = "$context.identity.sourceIp"
  }
}
resource "aws_apigatewayv2_route" "web" {
  count     = var.public_web_enabled ? 1 : 0
  api_id    = aws_apigatewayv2_api.web[0].id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.web[0].id}"
}
resource "aws_apigatewayv2_stage" "web" {
  count       = var.public_web_enabled ? 1 : 0
  api_id      = aws_apigatewayv2_api.web[0].id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 100
    throttling_rate_limit  = 50
  }
  # No request-body, credential, cookie or query logging.
}
output "public_web_url" {
  value = var.public_web_enabled ? aws_apigatewayv2_api.web[0].api_endpoint : null
}
output "public_web_private_ip" {
  value = var.public_web_enabled ? aws_instance.orion.private_ip : null
}
