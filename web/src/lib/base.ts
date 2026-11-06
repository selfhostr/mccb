/** A file of web/public, under the base of the build ('/' in the product, '/demo/' in the static demo). */
export const asset = (name: string): string => `${import.meta.env.BASE_URL}${name}`;
