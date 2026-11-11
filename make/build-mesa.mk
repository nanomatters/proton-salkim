# RADV is private to Proton and selected only by PROTON_RADV=1.
MESA_ARCHS := $(filter i386 x86_64,$(unix_ARCHS))
MESA_DEPENDS := wayland-protocols
MESA_LDFLAGS := -Wl,--exclude-libs,libdrm.a:libdrm_amdgpu.a

ifeq ($(WITHOUT_STEAMRT_DEPENDS),1)
include $(SRC)/steamrtdeps/build-wayland-protocols.mk
endif

include $(SRC)/make/mesa-xcb.mk

MESA_MESON_ARGS := \
	--buildtype=release --wrap-mode=nodownload \
	--force-fallback-for=libdrm,libdrm_amdgpu \
	-Dallow-fallback-for=libdrm \
	-Db_lto=false -Db_ndebug=true \
	-Dvulkan-drivers=amd -Dgallium-drivers= -Dplatforms=x11,wayland \
	-Dllvm=disabled -Damd-options= \
	-Dopengl=false -Dglx=disabled -Degl=disabled -Dgbm=disabled \
	-Dgles1=disabled -Dgles2=disabled -Dxlib-lease=disabled \
	-Dvulkan-layers= -Dtools= \
	-Dbuild-tests=false -Dbuild-radv-tests=false -Dbuild-aco-tests=false \
	-Dvalgrind=disabled -Dlibunwind=disabled -Dlmsensors=disabled -Ddisplay-info=disabled -Dspirv-tools=disabled \
	-Dlibdrm:default_library=static -Dlibdrm:b_staticpic=true \
	-Dlibdrm:auto_features=disabled -Dlibdrm:amdgpu=enabled \
	-Dlibdrm:intel=disabled -Dlibdrm:radeon=disabled \
	-Dlibdrm:nouveau=disabled -Dlibdrm:vmwgfx=disabled \
	-Dlibdrm:tests=false -Dlibdrm:install-test-programs=false

$(eval $(call rules-source,mesa-upstream,$(SRCDIR)/mesa))
MESA_SRC := $(OBJ)/src-mesa
MESA_ORIGIN := $(MESA_UPSTREAM_ORIGIN)

MESA_LIBDRM := $(MESA_SRC)/subprojects/libdrm-2.4.134
MESA_LIBDRM_ARCHIVE := $(MESA_XCB_DOWNLOADS)/libdrm-2.4.134.tar.xz
MESA_LIBDRM_PATCH := $(SRCDIR)/patches/libdrm/amdgpu-use-augmented-va-tree.patch
MESA_LIBDRM_WRAP := $(SRCDIR)/make/libdrm.wrap

$(MESA_LIBDRM_ARCHIVE):
	mkdir -p "$(dir $@)"
	curl --fail --location --retry 3 --output "$@.tmp" \
	    https://dri.freedesktop.org/libdrm/libdrm-2.4.134.tar.xz
	printf '%s  %s\n' ac5e74d157830eb8bee44c6a6bf3ad49774ef0dd2a72bdad74a8f20308b52a95 "$@.tmp" | sha256sum --check
	mv "$@.tmp" "$@"

$(OBJ)/.mesa-post-source: $(OBJ)/.mesa-upstream-post-source $(SRCDIR)/make/build-mesa.mk $(MESA_LIBDRM_ARCHIVE) $(MESA_LIBDRM_PATCH) $(MESA_LIBDRM_WRAP)
	rsync -arc --delete --exclude=/subprojects/libdrm-2.4.134 "$(MESA_UPSTREAM_SRC)/" "$(MESA_SRC)/"
	mkdir -p "$(MESA_LIBDRM)"
	tar -xf "$(MESA_LIBDRM_ARCHIVE)" --strip-components=1 -C "$(MESA_LIBDRM)"
	patch --batch --forward --fuzz=0 -d "$(MESA_LIBDRM)" -p1 < "$(MESA_LIBDRM_PATCH)"
	cp "$(MESA_LIBDRM_WRAP)" "$(MESA_SRC)/subprojects/libdrm.wrap"
	touch "$@"

$(OBJ)/.mesa-source: $(OBJ)/.mesa-post-source
	touch "$@"

.PHONY: mesa-source
mesa-source: $(OBJ)/.mesa-post-source
all-source: mesa-source

define mesa-arch-rules
$(call rules-meson,mesa,$(1),unix)

MESA_$(1)_ENV += \
	PKG_CONFIG_PATH="$$(MESA_XCB_$(1)_PKG_CONFIG_PATH):$$(WAYLAND_PROTOCOLS_$(1)_DST)/share/pkgconfig" \
	MESA_GIT_SHA1_OVERRIDE="$$(shell git -C $$(MESA_ORIGIN) rev-parse HEAD)"

$$(OBJ)/.mesa-$(1)-configure: $$(OBJ)/.mesa-xcb-$(1)-build $$(OBJ)/.mesa-post-source $$(SRCDIR)/make/build-mesa.mk

$$(OBJ)/.mesa-$(1)-dist: $$(SRCDIR)/make/mesa-package.py
	python3 "$$(SRCDIR)/make/mesa-package.py" install \
	    "$$(MESA_$(1)_DST)/lib/$$($(1)-unix_LIBDIR)/libvulkan_radeon.so" \
	    "$$(MESA_$(1)_DST)/share/vulkan/icd.d/radeon_icd.$$($(1)_MESON_CPU).json" \
	    "$$(DST_DIR)" $(1)
	$$(call install-strip,$$(MESA_$(1)_DST)/lib/$$($(1)-unix_LIBDIR)/libvulkan_radeon.so,$$(DST_LIBDIR)/mesa/$$($(1)-unix_LIBDIR))
	touch "$$@"
endef

$(foreach arch,$(MESA_ARCHS),$(eval $(call mesa-arch-rules,$(arch))))

$(OBJ)/.mesa-metadata: $(addprefix $(OBJ)/.mesa-,$(addsuffix -dist,$(MESA_ARCHS))) $(SRCDIR)/make/mesa-package.py
	python3 "$(SRCDIR)/make/mesa-package.py" metadata \
	    "$(MESA_ORIGIN)" "$(DST_DIR)" $(MESA_ARCHS)
	touch "$@"

$(OBJ)/.mesa-notices: $(OBJ)/.mesa-post-source $(SRCDIR)/make/mesa-notices.py $(SRCDIR)/make/licenses/mesa-blake3.txt
	python3 "$(SRCDIR)/make/mesa-notices.py" \
	    "$(MESA_SRC)" "$(MESA_LIBDRM)" "$(DST_BASE)/licenses/mesa" \
	    --wayland-protocols "$(SRCDIR)/steamrtdeps/wayland-protocols"
	touch "$@"

$(OBJ)/.mesa-data: $(OBJ)/.mesa-post-source $(SRCDIR)/make/mesa-package.py
	python3 "$(SRCDIR)/make/mesa-package.py" data "$(MESA_SRC)" "$(MESA_LIBDRM)" "$(DST_DIR)"
	touch "$@"

mesa-dist: $(OBJ)/.mesa-metadata $(OBJ)/.mesa-notices $(OBJ)/.mesa-data
mesa: mesa-dist
all-dist: mesa-dist

mesa-clean:: mesa-upstream-clean
	rm -rf "$(MESA_SRC)"
	rm -f $(addprefix $(OBJ)/.mesa-,source post-source metadata notices data)

mesa-distclean:: mesa-clean mesa-xcb-distclean
clean: mesa-clean
distclean: mesa-distclean
