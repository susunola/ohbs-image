package main

import (
	"context"
	"log"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"
	"github.com/susunola/terraform-provider-ohbsimage/internal/provider"
)

var version = "dev"

func main() {
	err := providerserver.Serve(context.Background(), provider.New(version),
		providerserver.ServeOpts{Address: "registry.terraform.io/susunola/ohbsimage"})
	if err != nil {
		log.Fatal(err)
	}
}
